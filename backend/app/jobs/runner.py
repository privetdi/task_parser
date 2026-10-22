"""Запуск задач: журнал, изоляция ошибок и защита от параллельного запуска одной задачи."""
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db import SessionFactory, session_scope
from app.jobs.deps import Dependencies
from app.models import JobRun, JobStatus, JobTrigger
from app.services.result import JobResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobDefinition:
    name: str
    title: str  # для интерфейса
    description: str
    run: Callable[[Dependencies, Session, JobTrigger], JobResult]
    # Имя настройки с cron-выражением; None — у задачи нет расписания
    cron_setting: str | None = None


class UnknownJob(KeyError):
    """Задачи с таким именем нет."""


class JobAlreadyRunning(Exception):
    """Эта задача уже выполняется — второй запуск не стартует."""


class JobRunner:
    def __init__(self, deps: Dependencies, jobs: list[JobDefinition], max_workers: int = 4) -> None:
        self.deps = deps
        self.jobs = {job.name: job for job in jobs}
        self._locks = {name: threading.Lock() for name in self.jobs}
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._stop = threading.Event()
        deps.should_stop = self._stop.is_set

    def is_running(self, name: str) -> bool:
        return self._lock(name).locked()

    def run_now(self, name: str, trigger: JobTrigger) -> int:
        """Выполняет задачу в текущем потоке (так её запускает расписание); возвращает id."""
        run_id, acquired = self._begin(name, trigger)
        if acquired:
            self._execute(name, run_id, trigger)
        return run_id

    def start_async(self, name: str, trigger: JobTrigger = JobTrigger.MANUAL) -> int:
        """Запускает задачу в фоне и сразу возвращает id запуска (ручной запуск из интерфейса)."""
        run_id, acquired = self._begin(name, trigger)
        if acquired:
            self._pool.submit(self._execute, name, run_id, trigger)
        return run_id

    def shutdown(self) -> None:
        """Просит выполняющиеся задачи остановиться, иначе Ctrl+C ждал бы конца долгой задачи."""
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- внутреннее ---

    def _lock(self, name: str) -> threading.Lock:
        try:
            return self._locks[name]
        except KeyError:
            raise UnknownJob(name) from None

    def _begin(self, name: str, trigger: JobTrigger) -> tuple[int, bool]:
        """Занимает задачу и пишет в журнал «выполняется». (id запуска, запустилась ли задача)."""
        lock = self._lock(name)
        if not lock.acquire(blocking=False):
            if trigger == JobTrigger.MANUAL:
                raise JobAlreadyRunning(name)
            return self._record_skipped(name, trigger), False
        try:
            with session_scope(self.deps.session_factory) as session:
                run = JobRun(
                    job=name, trigger=trigger, status=JobStatus.RUNNING, started_at=self.deps.now()
                )
                session.add(run)
                session.flush()
                return run.id, True
        except Exception:
            lock.release()
            raise

    def _record_skipped(self, name: str, trigger: JobTrigger) -> int:
        now = self.deps.now()
        with session_scope(self.deps.session_factory) as session:
            run = JobRun(
                job=name, trigger=trigger, status=JobStatus.SKIPPED, started_at=now,
                finished_at=now, summary="Пропущено: предыдущий запуск этой задачи ещё выполняется",
            )
            session.add(run)
            session.flush()
            return run.id

    def _execute(self, name: str, run_id: int, trigger: JobTrigger) -> None:
        job = self.jobs[name]
        try:
            try:
                with session_scope(self.deps.session_factory) as session:
                    result = job.run(self.deps, session, trigger)
            except Exception as exc:  # задача не должна ронять ни сервис, ни другие задачи
                log.exception("Задача %s завершилась исключением", name)
                text = f"{type(exc).__name__}: {exc}"
                result = JobResult(JobStatus.ERROR, f"Непредвиденная ошибка: {text}", [text])
            self._finish(run_id, result)
        except Exception:
            log.exception("Не удалось записать итог запуска %s задачи %s", run_id, name)
        finally:
            self._locks[name].release()

    def _finish(self, run_id: int, result: JobResult) -> None:
        with session_scope(self.deps.session_factory) as session:
            run = session.get(JobRun, run_id)
            run.status = result.status
            run.finished_at = self.deps.now()
            run.summary = result.summary
            run.error = "\n".join(result.errors) or None


def mark_interrupted_runs(session_factory: SessionFactory, now: datetime) -> int:
    """Запуски, оставшиеся «выполняется» после остановки сервиса, помечает как прерванные."""
    with session_scope(session_factory) as session:
        result = session.execute(
            update(JobRun)
            .where(JobRun.status == JobStatus.RUNNING)
            .values(
                status=JobStatus.INTERRUPTED,
                finished_at=now,
                error="Запуск прерван остановкой сервиса",
            )
        )
        return result.rowcount
