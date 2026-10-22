from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionFactory
from app.jobs import tasks
from app.jobs.deps import Dependencies
from app.jobs.registry import JOBS
from app.jobs.runner import JobRunner
from app.models import (
    Chart,
    Currency,
    Delivery,
    DeliveryKind,
    ExchangeRate,
    JobRun,
    JobStatus,
    JobTrigger,
)
from app.sources.iea import SiteBlockedError
from app.sources.nbrb import NbrbError
from tests.fakes import FakeNbrb, FakeSource, ManagedIea
from tests.helpers import RecordingMailer

# 06:00 UTC = 09:00 в Минске: «сегодня» по часовому поясу из настроек — 2026-10-22
NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
TODAY = date(2026, 10, 22)


@pytest.fixture
def mailer() -> RecordingMailer:
    return RecordingMailer()


@pytest.fixture
def nbrb() -> FakeNbrb:
    return FakeNbrb()


@pytest.fixture
def iea() -> ManagedIea:
    return ManagedIea(FakeSource("a", "b"))


@pytest.fixture
def deps(
    session_factory: SessionFactory, mailer: RecordingMailer, nbrb: FakeNbrb, iea: ManagedIea,
    tmp_path: Path,
) -> Dependencies:
    settings = Settings(_env_file=None, data_dir=tmp_path, backfill_start=date(2026, 9, 1))
    return Dependencies(
        settings=settings, session_factory=session_factory, nbrb_client=lambda: nbrb,
        mailer=lambda: mailer, iea_client=iea, clock=lambda: NOW,
    )


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_backfill_loads_catalog_then_history_and_closes_client(
    deps: Dependencies, session: Session, nbrb: FakeNbrb
) -> None:
    result = tasks.rates_backfill(deps, session, JobTrigger.STARTUP)

    assert result.status == JobStatus.SUCCESS and "добавлено" in result.summary
    assert count(session, Currency) == 3
    # USD: 2026-09-01..2025-12-31 не пересекается со старой версией -> только новая версия + EUR
    assert session.scalar(select(func.max(ExchangeRate.rate_date))) == TODAY
    assert nbrb.closed


def test_second_backfill_has_nothing_to_do(deps: Dependencies, session: Session) -> None:
    tasks.rates_backfill(deps, session, JobTrigger.STARTUP)
    before = count(session, ExchangeRate)

    result = tasks.rates_backfill(deps, session, JobTrigger.MANUAL)

    assert "полностью" in result.summary and count(session, ExchangeRate) == before


def test_catalog_failure_is_an_explained_error_and_client_is_still_closed(
    deps: Dependencies, session: Session, nbrb: FakeNbrb
) -> None:
    nbrb.catalog_error = NbrbError("справочник валют: HTTP 503 (попыток: 3)")

    for task in (tasks.rates_backfill, tasks.rates_sync):
        nbrb.closed = False
        result = task(deps, session, JobTrigger.SCHEDULE)

        assert result.status == JobStatus.ERROR
        assert "Справочник валют не загружен" in result.summary and "503" in result.errors[0]
        assert nbrb.closed


def test_daily_sync_after_backfill_reports_no_changes(deps: Dependencies, session: Session) -> None:
    tasks.rates_backfill(deps, session, JobTrigger.STARTUP)

    result = tasks.rates_sync(deps, session, JobTrigger.SCHEDULE)

    assert result.status == JobStatus.SUCCESS and "Без изменений" in result.summary


def test_report_by_schedule_goes_once_but_manual_always_goes(
    deps: Dependencies, session: Session, mailer: RecordingMailer
) -> None:
    tasks.rates_backfill(deps, session, JobTrigger.STARTUP)

    first = tasks.rates_report(deps, session, JobTrigger.SCHEDULE)
    again = tasks.rates_report(deps, session, JobTrigger.SCHEDULE)
    manual = tasks.rates_report(deps, session, JobTrigger.MANUAL)

    assert [first.status, again.status, manual.status] == [
        JobStatus.SUCCESS, JobStatus.SKIPPED, JobStatus.SUCCESS,
    ]
    assert len(mailer.sent) == 2
    assert "на 22.10.2026" in mailer.sent[0].subject  # дата — по часовому поясу из настроек


def test_report_without_loaded_rates_explains_what_to_do(
    deps: Dependencies, session: Session, mailer: RecordingMailer
) -> None:
    result = tasks.rates_report(deps, session, JobTrigger.SCHEDULE)

    assert result.status == JobStatus.ERROR and mailer.sent == []


def test_iea_job_sends_charts_into_settings_directory_and_closes_browser(
    deps: Dependencies, session: Session, mailer: RecordingMailer, iea: ManagedIea, tmp_path: Path
) -> None:
    result = tasks.iea_charts(deps, session, JobTrigger.SCHEDULE)

    assert result.status == JobStatus.SUCCESS and len(mailer.sent) == 2
    assert (tmp_path / "charts" / "a.png").is_file()
    assert iea.entered and iea.exited


def test_blocked_iea_is_an_error_run_that_leaves_other_jobs_untouched(
    deps: Dependencies, session: Session, mailer: RecordingMailer, iea: ManagedIea
) -> None:
    iea.source.fetch_error = SiteBlockedError("сайт отдал проверку на робота")

    result = tasks.iea_charts(deps, session, JobTrigger.SCHEDULE)

    assert result.status == JobStatus.ERROR and "проверку на робота" in result.summary
    assert iea.exited and mailer.sent == []
    assert count(session, Chart) == 0


def test_all_registered_jobs_run_through_the_runner_and_are_journaled(
    deps: Dependencies, session: Session, mailer: RecordingMailer
) -> None:
    runner = JobRunner(deps, JOBS)

    for job in JOBS:
        runner.run_now(job.name, JobTrigger.MANUAL)

    session.expire_all()
    journal = {run.job: run for run in session.scalars(select(JobRun))}
    assert set(journal) == {"rates_backfill", "rates_sync", "rates_report", "iea_charts"}
    assert {run.status for run in journal.values()} == {JobStatus.SUCCESS}
    assert all(run.trigger == JobTrigger.MANUAL and run.finished_at for run in journal.values())
    assert count(session, Delivery) == 3  # отчёт + два графика
    assert session.scalar(
        select(func.count()).select_from(Delivery).where(Delivery.kind == DeliveryKind.IEA_CHART)
    ) == 2
