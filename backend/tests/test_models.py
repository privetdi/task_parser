from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    Chart,
    ChartStatus,
    Currency,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    ExchangeRate,
    JobRun,
    JobStatus,
    JobTrigger,
)


def make_currency(cur_id: int = 431) -> Currency:
    return Currency(
        cur_id=cur_id, parent_id=145, abbreviation="USD", name="Доллар США",
        scale=1, periodicity=0, date_start=date(2021, 7, 9), date_end=date(2050, 1, 1),
    )


def test_schema_is_created_in_empty_db(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert tables == {"currencies", "exchange_rates", "job_runs", "charts", "deliveries"}


def test_duplicate_rate_for_same_currency_and_date_is_rejected(session: Session) -> None:
    session.add(make_currency())
    session.add(ExchangeRate(currency_id=431, rate_date=date(2026, 1, 1), official_rate=3.0))
    session.commit()

    session.add(ExchangeRate(currency_id=431, rate_date=date(2026, 1, 1), official_rate=3.1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_date_for_different_currency_versions_is_allowed(session: Session) -> None:
    session.add_all([make_currency(145), make_currency(431)])
    session.add_all([
        ExchangeRate(currency_id=145, rate_date=date(2021, 7, 8), official_rate=2.5),
        ExchangeRate(currency_id=431, rate_date=date(2021, 7, 8), official_rate=2.5),
    ])
    session.commit()


def test_rate_for_unknown_currency_is_rejected(session: Session) -> None:
    session.add(ExchangeRate(currency_id=999, rate_date=date(2026, 1, 1), official_rate=1.0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_delivery_key_is_rejected(session: Session) -> None:
    for _ in range(2):
        session.add(Delivery(
            kind=DeliveryKind.RATES_REPORT, key="2026-01-01", status=DeliveryStatus.SENT,
        ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_key_for_different_delivery_kinds_is_allowed(session: Session) -> None:
    session.add_all([
        Delivery(kind=DeliveryKind.RATES_REPORT, key="x", status=DeliveryStatus.SENT),
        Delivery(kind=DeliveryKind.IEA_CHART, key="x", status=DeliveryStatus.SENT),
    ])
    session.commit()


def test_duplicate_chart_slug_is_rejected(session: Session) -> None:
    for _ in range(2):
        session.add(Chart(
            slug="charts/a", title="A", url="https://example.com/a", status=ChartStatus.DISCOVERED,
        ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_datetimes_roundtrip_as_utc(session: Session) -> None:
    moment = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    session.add(JobRun(
        job="rates_sync", trigger=JobTrigger.MANUAL, status=JobStatus.RUNNING, started_at=moment,
    ))
    session.commit()
    session.expire_all()

    run = session.scalars(select(JobRun)).one()
    assert run.started_at == moment
    assert run.started_at.tzinfo is not None


def test_naive_datetime_is_rejected(session: Session) -> None:
    session.add(JobRun(
        job="rates_sync", trigger=JobTrigger.MANUAL, status=JobStatus.RUNNING,
        started_at=datetime(2026, 10, 22, 9, 0),
    ))
    # SQLAlchemy оборачивает ValueError из типа столбца в StatementError
    with pytest.raises(StatementError, match="часовым поясом"):
        session.commit()


def test_enums_are_stored_as_values(session: Session) -> None:
    session.add(JobRun(job="x", trigger=JobTrigger.SCHEDULE, status=JobStatus.SUCCESS))
    session.commit()
    stored = session.execute(text("SELECT trigger, status FROM job_runs")).one()
    assert tuple(stored) == ("schedule", "success")
