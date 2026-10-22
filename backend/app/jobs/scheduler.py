"""Расписание: запускает задачи по cron-выражениям из настроек."""
import logging
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.db import session_scope
from app.jobs.runner import JobRunner
from app.models import JobTrigger
from app.services.rates_sync import backfill_progress

log = logging.getLogger(__name__)

BACKFILL_JOB = "rates_backfill"


@dataclass(frozen=True)
class ScheduleInfo:
    cron: str | None  # None — у задачи нет расписания (запускается при старте и вручную)
    next_run: datetime | None


class JobScheduler:
    def __init__(self, runner: JobRunner, settings: Settings) -> None:
        self._runner = runner
        self._settings = settings
        self._crons = {
            job.name: getattr(settings, job.cron_setting)
            for job in runner.jobs.values()
            if job.cron_setting
        }
        self._scheduler = BackgroundScheduler(timezone=settings.tz)

    def start(self) -> None:
        for job in self._runner.jobs.values():
            if job.name not in self._crons:
                continue
            self._scheduler.add_job(
                self._runner.run_now,
                trigger=CronTrigger.from_crontab(self._crons[job.name], timezone=self._settings.tz),
                args=[job.name, JobTrigger.SCHEDULE],
                id=job.name,
                name=job.title,
                max_instances=1,
                # Если запуск задержался (занят планировщик), всё равно выполнить, но один раз
                coalesce=True,
                misfire_grace_time=3600,
            )
        self._scheduler.start()
        log.info("Планировщик запущен: %s", self._crons)
        if self._settings.backfill_on_startup and self._backfill_needed():
            self._runner.start_async(BACKFILL_JOB, JobTrigger.STARTUP)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def info(self, name: str) -> ScheduleInfo:
        job = self._scheduler.get_job(name)
        return ScheduleInfo(
            cron=self._crons.get(name),
            next_run=job.next_run_time if job is not None else None,
        )

    def _backfill_needed(self) -> bool:
        if BACKFILL_JOB not in self._runner.jobs:
            return False
        deps = self._runner.deps
        with session_scope(deps.session_factory) as session:
            progress = backfill_progress(
                session, today=deps.today(), start=self._settings.backfill_start
            )
        return not progress.is_complete
