import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionFactory
from app.jobs.deps import Dependencies
from app.jobs.registry import JOBS
from app.jobs.runner import (
    JobAlreadyRunning,
    JobDefinition,
    JobRunner,
    UnknownJob,
    mark_interrupted_runs,
)
from app.models import Chart, ChartStatus, JobRun, JobStatus, JobTrigger
from app.services.result import JobResult

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def make_deps(session_factory: SessionFactory) -> Dependencies:
    def unused():
        raise AssertionError("внешний клиент в этом тесте не нужен")

    return Dependencies(
        settings=Settings(_env_file=None),
        session_factory=session_factory,
        nbrb_client=unused,
        mailer=unused,
        iea_client=unused,
        clock=lambda: NOW,
    )


def make_runner(session_factory: SessionFactory, *jobs: JobDefinition) -> JobRunner:
    return JobRunner(make_deps(session_factory), list(jobs))


def job(name: str, run) -> JobDefinition:
    return JobDefinition(name=name, title=name, description="", run=run)


def runs(session: Session) -> list[JobRun]:
    session.expire_all()
    return list(session.scalars(select(JobRun).order_by(JobRun.id)))


def test_successful_run_is_journaled_with_summary_and_trigger(
    session_factory: SessionFactory, session: Session
) -> None:
    runner = make_runner(
        session_factory, job("ok", lambda deps, s, t: JobResult(JobStatus.SUCCESS, "Загружено 5"))
    )

    run_id = runner.run_now("ok", JobTrigger.SCHEDULE)

    (run,) = runs(session)
    assert run.id == run_id and run.job == "ok"
    assert run.status == JobStatus.SUCCESS and run.summary == "Загружено 5"
    assert run.trigger == JobTrigger.SCHEDULE
    assert run.started_at == NOW and run.finished_at == NOW and run.error is None


def test_partial_result_keeps_all_error_messages(
    session_factory: SessionFactory, session: Session
) -> None:
    runner = make_runner(session_factory, job(
        "p", lambda deps, s, t: JobResult(JobStatus.PARTIAL, "частично", ["USD: сбой", "EUR: сбой"])
    ))

    runner.run_now("p", JobTrigger.MANUAL)

    (run,) = runs(session)
    assert run.status == JobStatus.PARTIAL
    assert run.error == "USD: сбой\nEUR: сбой"


def test_job_receives_dependencies_session_and_trigger(session_factory: SessionFactory) -> None:
    seen = {}

    def spy(deps, session, trigger):
        seen.update(deps=deps, session=type(session).__name__, trigger=trigger)
        return JobResult(JobStatus.SUCCESS, "")

    runner = make_runner(session_factory, job("spy", spy))
    runner.run_now("spy", JobTrigger.MANUAL)

    assert seen["deps"] is runner.deps and seen["trigger"] == JobTrigger.MANUAL
    assert seen["session"] == "Session"


def test_exception_becomes_error_run_and_is_not_raised(
    session_factory: SessionFactory, session: Session
) -> None:
    def boom(deps, s, trigger):
        raise RuntimeError("всё сломалось")

    runner = make_runner(session_factory, job("boom", boom))

    runner.run_now("boom", JobTrigger.SCHEDULE)  # не должно бросить

    (run,) = runs(session)
    assert run.status == JobStatus.ERROR
    assert "RuntimeError: всё сломалось" in run.summary and "всё сломалось" in run.error
    assert run.finished_at is not None


def test_failing_job_does_not_affect_another_job(
    session_factory: SessionFactory, session: Session
) -> None:
    def boom(deps, s, trigger):
        raise RuntimeError("сбой")

    runner = make_runner(
        session_factory,
        job("boom", boom),
        job("ok", lambda deps, s, t: JobResult(JobStatus.SUCCESS, "жив")),
    )

    runner.run_now("boom", JobTrigger.SCHEDULE)
    runner.run_now("ok", JobTrigger.SCHEDULE)
    runner.run_now("boom", JobTrigger.SCHEDULE)

    assert [(r.job, r.status) for r in runs(session)] == [
        ("boom", JobStatus.ERROR), ("ok", JobStatus.SUCCESS), ("boom", JobStatus.ERROR),
    ]


def test_database_changes_of_a_crashed_job_are_rolled_back(
    session_factory: SessionFactory, session: Session
) -> None:
    def half_done(deps, s, trigger):
        s.add(Chart(slug="x", title="X", url="u", status=ChartStatus.DISCOVERED))
        s.flush()
        raise RuntimeError("упал посередине")

    make_runner(session_factory, job("half", half_done)).run_now("half", JobTrigger.MANUAL)

    assert session.scalars(select(Chart)).all() == []


def test_second_manual_start_of_running_job_is_refused_and_job_can_run_again_later(
    session_factory: SessionFactory, session: Session
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(deps, s, trigger):
        started.set()
        release.wait(timeout=5)
        return JobResult(JobStatus.SUCCESS, "готово")

    runner = make_runner(session_factory, job("slow", slow))
    first_id = runner.start_async("slow")
    assert started.wait(timeout=5)

    assert runner.is_running("slow")
    with pytest.raises(JobAlreadyRunning):
        runner.start_async("slow")
    (running,) = runs(session)
    assert running.id == first_id and running.status == JobStatus.RUNNING  # отказ не создал записей

    release.set()
    _wait_until(lambda: not runner.is_running("slow"))
    assert runs(session)[0].status == JobStatus.SUCCESS
    runner.run_now("slow", JobTrigger.MANUAL)  # после завершения запуск снова возможен
    assert len(runs(session)) == 2


def test_scheduled_trigger_on_busy_job_is_journaled_as_skipped(
    session_factory: SessionFactory, session: Session
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(deps, s, trigger):
        started.set()
        release.wait(timeout=5)
        return JobResult(JobStatus.SUCCESS, "")

    runner = make_runner(session_factory, job("slow", slow))
    runner.start_async("slow")
    assert started.wait(timeout=5)

    skipped_id = runner.run_now("slow", JobTrigger.SCHEDULE)

    skipped = next(r for r in runs(session) if r.id == skipped_id)
    assert skipped.status == JobStatus.SKIPPED and "ещё выполняется" in skipped.summary
    release.set()
    _wait_until(lambda: not runner.is_running("slow"))


def test_async_start_returns_immediately_with_running_record(
    session_factory: SessionFactory, session: Session
) -> None:
    release = threading.Event()

    def slow(deps, s, trigger):
        release.wait(timeout=5)
        return JobResult(JobStatus.SUCCESS, "")

    runner = make_runner(session_factory, job("slow", slow))
    run_id = runner.start_async("slow")

    (run,) = runs(session)
    assert run.id == run_id and run.status == JobStatus.RUNNING and run.trigger == JobTrigger.MANUAL
    assert run.finished_at is None
    release.set()
    _wait_until(lambda: not runner.is_running("slow"))


def test_unknown_job_is_rejected(session_factory: SessionFactory) -> None:
    runner = make_runner(session_factory)
    with pytest.raises(UnknownJob):
        runner.start_async("nope")
    with pytest.raises(UnknownJob):
        runner.is_running("nope")


def test_runs_left_running_after_a_stop_are_marked_interrupted(
    session_factory: SessionFactory, session: Session
) -> None:
    session.add_all([
        JobRun(job="a", trigger=JobTrigger.SCHEDULE, status=JobStatus.RUNNING, started_at=NOW),
        JobRun(job="b", trigger=JobTrigger.MANUAL, status=JobStatus.SUCCESS, started_at=NOW),
    ])
    session.commit()
    later = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    count = mark_interrupted_runs(session_factory, later)

    a, b = runs(session)
    assert count == 1
    assert a.status == JobStatus.INTERRUPTED and a.finished_at == later
    assert "остановкой" in a.error
    assert b.status == JobStatus.SUCCESS and b.finished_at is None  # завершённые не трогаем


def test_registry_has_the_four_jobs_and_their_cron_settings_exist() -> None:
    assert [j.name for j in JOBS] == ["rates_backfill", "rates_sync", "rates_report", "iea_charts"]
    settings = Settings(_env_file=None)
    scheduled = {j.name: getattr(settings, j.cron_setting) for j in JOBS if j.cron_setting}
    assert set(scheduled) == {"rates_sync", "rates_report", "iea_charts"}
    assert next(j for j in JOBS if j.name == "rates_backfill").cron_setting is None


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        deadline.wait(0.01)
    raise AssertionError("условие не выполнилось за отведённое время")


def test_shutdown_asks_running_jobs_to_stop_and_the_run_is_recorded_as_interrupted(
    session_factory: SessionFactory, session: Session
) -> None:
    started = threading.Event()

    def until_stopped(deps, s, trigger):
        started.set()
        while not deps.should_stop():
            threading.Event().wait(0.005)
        return JobResult(JobStatus.INTERRUPTED, "Остановлено вместе с сервисом")

    runner = make_runner(session_factory, job("long", until_stopped))
    assert runner.deps.should_stop() is False
    runner.start_async("long")
    assert started.wait(timeout=5)

    runner.shutdown()  # то, что происходит при остановке сервиса

    assert runner.deps.should_stop() is True
    _wait_until(lambda: not runner.is_running("long"))
    (run,) = runs(session)
    assert run.status == JobStatus.INTERRUPTED and run.finished_at is not None
