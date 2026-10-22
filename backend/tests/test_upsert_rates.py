from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate
from app.services.rates_sync import UpsertCounts, upsert_rates
from app.sources.nbrb import RatePoint

T1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def currencies(session: Session) -> None:
    for cur_id in (145, 431):
        session.add(Currency(
            cur_id=cur_id, parent_id=145, abbreviation="USD", name="Доллар США", scale=1,
            periodicity=0, date_start=date(2016, 7, 1), date_end=date(2050, 1, 1),
        ))
    session.commit()


def points(*pairs: tuple[int, float]) -> list[RatePoint]:
    return [RatePoint(date(2026, 10, day), rate) for day, rate in pairs]


def count_rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ExchangeRate))


def test_new_rates_are_added(session: Session) -> None:
    counts = upsert_rates(session, 431, points((1, 3.0), (2, 3.1), (3, 3.2)), T1)

    assert counts == UpsertCounts(added=3, updated=0, unchanged=0)
    assert count_rows(session) == 3


def test_repeating_the_same_load_changes_nothing(session: Session) -> None:
    upsert_rates(session, 431, points((1, 3.0), (2, 3.1)), T1)
    counts = upsert_rates(session, 431, points((1, 3.0), (2, 3.1)), T2)

    assert counts == UpsertCounts(added=0, updated=0, unchanged=2)
    assert count_rows(session) == 2


def test_changed_value_is_updated_and_only_it(session: Session) -> None:
    upsert_rates(session, 431, points((1, 3.0), (2, 3.1)), T1)
    counts = upsert_rates(session, 431, points((1, 3.0), (2, 3.15)), T2)

    assert counts == UpsertCounts(added=0, updated=1, unchanged=1)
    rows = {r.rate_date.day: r for r in session.scalars(select(ExchangeRate))}
    assert rows[2].official_rate == 3.15
    assert rows[2].updated_at == T2
    assert rows[1].updated_at == T1  # неизменённую запись не трогали


def test_float_noise_below_compare_precision_is_not_a_change(session: Session) -> None:
    upsert_rates(session, 431, points((1, 3.0)), T1)
    counts = upsert_rates(session, 431, points((1, 3.0 + 1e-9)), T2)

    assert counts.unchanged == 1 and counts.updated == 0


def test_new_and_changed_in_one_batch(session: Session) -> None:
    upsert_rates(session, 431, points((1, 3.0)), T1)
    counts = upsert_rates(session, 431, points((1, 3.5), (2, 3.1)), T2)

    assert counts == UpsertCounts(added=1, updated=1, unchanged=0)


def test_same_date_for_different_currency_versions_is_independent(session: Session) -> None:
    upsert_rates(session, 145, points((1, 2.5)), T1)
    counts = upsert_rates(session, 431, points((1, 2.6)), T1)

    assert counts.added == 1
    assert count_rows(session) == 2


def test_duplicate_date_in_input_uses_last_value(session: Session) -> None:
    counts = upsert_rates(session, 431, points((1, 3.0), (1, 3.3)), T1)

    assert counts.added == 1
    assert session.scalars(select(ExchangeRate.official_rate)).one() == 3.3


def test_empty_input_is_noop(session: Session) -> None:
    assert upsert_rates(session, 431, [], T1) == UpsertCounts()
    assert count_rows(session) == 0


def test_counts_can_be_summed() -> None:
    total = UpsertCounts(added=1)
    total += UpsertCounts(added=2, updated=1, unchanged=4)
    assert total == UpsertCounts(added=3, updated=1, unchanged=4)
