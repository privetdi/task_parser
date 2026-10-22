import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionFactory
from app.jobs.deps import Dependencies
from app.jobs.registry import JOBS
from app.jobs.runner import JobDefinition, JobRunner
from app.jobs.scheduler import JobScheduler
from app.models import Currency, JobRun, JobStatus, JobTrigger
from app.services.result import JobResult

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)  # 09:00 в Минске


def make_deps(session_factory: SessionFactory, settings: Settings) -> Dependencies:
    def unused():
        raise AssertionError("внешний клиент в этом тесте не нужен")

    return Dependencies(
        settings=settings, session_factory=session_factory, nbrb_client=unused,
        mailer=unused, iea_client=unused, clock=lambda: NOW,
    )


def stub_jobs(calls: list[tuple[str, JobTrigger]]) -> list[JobDefinition]:
    """Реальный реестр, но тела задач заменены заглушками, которые просто фиксируют запуск."""

    def stub(name: str):
        def run(deps, session, trigger):
            calls.append((name, trigger))
            return JobResult(JobStatus.SUCCESS, f"заглушка {name}")

        return run

    return [
        JobDefinition(j.name, j.title, j.description, stub(j.name), j.cron_setting) for j in JOBS
    ]


class Harness:
    def __init__(self, session_factory: SessionFactory, **settings: object) -> None:
        self.settings = Settings(_env_file=None, **settings)
        self.calls: list[tuple[str, JobTrigger]] = []
        self.runner = JobRunner(
            make_deps(session_factory, self.settings), stub_jobs(self.calls)
        )
        self.scheduler = JobScheduler(self.runner, self.settings)

    def local(self, name: str) -> datetime:
        return self.scheduler.info(name).next_run.astimezone(self.settings.tz)


@pytest.fixture
def make(session_factory: SessionFactory) -> Iterator:
    started: list[Harness] = []

    def factory(**settings: object) -> Harness:
        harness = Harness(session_factory, **settings)
        started.append(harness)
        return harness

    yield factory
    for harness in started:
        harness.scheduler.shutdown()
        harness.runner.shutdown()


def wait_for(condition, timeout: float = 5.0) -> None:
    gate = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        gate.wait(0.01)
    raise AssertionError("условие не выполнилось за отведённое время")


def test_default_schedule_is_registered_in_minsk_time(make) -> None:
    h = make(backfill_on_startup=False)
    h.scheduler.start()

    for name, hour in [("rates_sync", 8), ("rates_report", 9), ("iea_charts", 10)]:
        next_run = h.local(name)
        assert (next_run.hour, next_run.minute) == (hour, 0), name
        assert str(next_run.tzinfo) == "Europe/Minsk"
    assert h.scheduler.info("rates_report").cron == "0 9 * * *"


def test_backfill_has_no_schedule(make) -> None:
    h = make(backfill_on_startup=False)
    h.scheduler.start()

    info = h.scheduler.info("rates_backfill")
    assert info.cron is None and info.next_run is None


def test_changing_the_setting_changes_the_schedule(make) -> None:
    h = make(backfill_on_startup=False, cron_rates_report="30 7 * * 1-5", timezone="Europe/Minsk")
    h.scheduler.start()

    next_run = h.local("rates_report")
    assert (next_run.hour, next_run.minute) == (7, 30)
    assert next_run.weekday() < 5
    assert h.scheduler.info("rates_report").cron == "30 7 * * 1-5"
    # Остальные расписания не пострадали
    assert h.local("rates_sync").hour == 8


def test_timezone_setting_is_respected(make) -> None:
    h = make(backfill_on_startup=False, timezone="UTC", cron_rates_sync="15 3 * * *")
    h.scheduler.start()

    next_run = h.scheduler.info("rates_sync").next_run
    assert str(next_run.tzinfo) == "UTC" and (next_run.hour, next_run.minute) == (3, 15)


def test_scheduled_firing_runs_the_job_with_schedule_trigger(
    make, session: Session
) -> None:
    h = make(backfill_on_startup=False)
    h.scheduler.start()

    # Вызываем то, что планировщик вызвал бы в назначенное время
    apscheduler_job = h.scheduler._scheduler.get_job("rates_sync")
    apscheduler_job.func(*apscheduler_job.args)

    assert h.calls == [("rates_sync", JobTrigger.SCHEDULE)]
    session.expire_all()
    (run,) = session.scalars(select(JobRun))
    assert run.job == "rates_sync" and run.trigger == JobTrigger.SCHEDULE
    assert run.status == JobStatus.SUCCESS


def test_backfill_starts_at_startup_when_history_is_not_loaded(make, session: Session) -> None:
    h = make(backfill_on_startup=True)

    h.scheduler.start()

    wait_for(lambda: h.calls == [("rates_backfill", JobTrigger.STARTUP)])
    session.expire_all()
    wait_for(lambda: session.scalars(select(JobRun)).first().finished_at is not None)


def test_backfill_is_not_started_when_disabled(make) -> None:
    h = make(backfill_on_startup=False)

    h.scheduler.start()

    assert h.calls == []


def test_backfill_is_not_started_when_history_is_already_complete(
    make, session: Session
) -> None:
    session.add(Currency(
        cur_id=431, parent_id=145, abbreviation="USD", name="USD", scale=1, periodicity=0,
        date_start=date(2026, 1, 1), date_end=date(2050, 1, 1),
        history_loaded_from=date(2026, 9, 1),
    ))
    session.commit()
    h = make(backfill_on_startup=True, backfill_start=date(2026, 9, 1))

    h.scheduler.start()

    assert h.calls == []


def test_backfill_resumes_when_history_is_incomplete(make, session: Session) -> None:
    session.add(Currency(
        cur_id=431, parent_id=145, abbreviation="USD", name="USD", scale=1, periodicity=0,
        date_start=date(2026, 1, 1), date_end=date(2050, 1, 1),
        history_loaded_from=date(2026, 10, 1),  # загружено не до границы 2026-09-01
    ))
    session.commit()
    h = make(backfill_on_startup=True, backfill_start=date(2026, 9, 1))

    h.scheduler.start()

    wait_for(lambda: h.calls == [("rates_backfill", JobTrigger.STARTUP)])


def test_shutdown_stops_the_scheduler(make) -> None:
    h = make(backfill_on_startup=False)
    h.scheduler.start()
    assert h.scheduler._scheduler.running

    h.scheduler.shutdown()

    assert not h.scheduler._scheduler.running
    h.scheduler.shutdown()  # повторная остановка безопасна
