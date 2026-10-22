from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate
from app.services.report import collect_report_rows

REPORT_DATE = date(2026, 10, 22)


def add_currency(
    session: Session, cur_id: int, parent_id: int, code: str, *, scale: int = 1,
    periodicity: int = 0, start: date = date(1991, 1, 1), end: date = date(2050, 1, 1),
) -> None:
    session.add(Currency(
        cur_id=cur_id, parent_id=parent_id, abbreviation=code, name=f"Валюта {code}",
        scale=scale, periodicity=periodicity, date_start=start, date_end=end,
    ))


def add_rate(session: Session, cur_id: int, day: date, rate: float) -> None:
    session.add(ExchangeRate(currency_id=cur_id, rate_date=day, official_rate=rate))


@pytest.fixture
def seeded(session: Session) -> Session:
    add_currency(session, 431, 145, "USD")
    add_rate(session, 431, date(2026, 10, 20), 2.99)
    add_rate(session, 431, date(2026, 10, 21), 2.9883)
    add_rate(session, 431, date(2026, 10, 22), 2.9713)
    session.commit()
    return session


def test_latest_rate_not_later_than_report_date_with_change(seeded: Session) -> None:
    (row,) = collect_report_rows(seeded, REPORT_DATE)

    assert (row.code, row.rate, row.rate_date) == ("USD", 2.9713, date(2026, 10, 22))
    assert row.change == pytest.approx(2.9713 - 2.9883)
    assert row.scale == 1 and row.periodicity == 0


def test_rate_published_for_tomorrow_is_not_in_todays_report(seeded: Session) -> None:
    add_rate(seeded, 431, date(2026, 10, 23), 3.5)
    seeded.commit()

    (row,) = collect_report_rows(seeded, REPORT_DATE)

    assert row.rate_date == date(2026, 10, 22) and row.rate == 2.9713


def test_report_for_earlier_date_uses_the_data_of_that_date(seeded: Session) -> None:
    (row,) = collect_report_rows(seeded, date(2026, 10, 21))

    assert row.rate_date == date(2026, 10, 21) and row.rate == 2.9883


def test_monthly_currency_shows_last_set_rate_with_its_own_date(session: Session) -> None:
    add_currency(session, 507, 226, "AZN", periodicity=1)
    add_rate(session, 507, date(2026, 9, 1), 1.7879)
    add_rate(session, 507, date(2026, 10, 1), 1.7769)
    session.commit()

    (row,) = collect_report_rows(session, REPORT_DATE)

    assert row.rate_date == date(2026, 10, 1)  # а не «пусто» за сегодня
    assert row.periodicity == 1
    assert row.change == pytest.approx(1.7769 - 1.7879)


def test_versions_of_one_currency_give_one_row_and_change_crosses_the_boundary(
    session: Session,
) -> None:
    add_currency(session, 145, 145, "USD", end=date(2021, 7, 8))
    add_currency(session, 431, 145, "USD", start=date(2021, 7, 9))
    add_rate(session, 145, date(2021, 7, 8), 2.5552)
    add_rate(session, 431, date(2021, 7, 9), 2.5921)
    session.commit()

    rows = collect_report_rows(session, date(2021, 7, 9))

    assert len(rows) == 1
    assert rows[0].rate == 2.5921
    assert rows[0].change == pytest.approx(2.5921 - 2.5552)


def test_change_is_compared_per_unit_when_scale_differs_between_versions(session: Session) -> None:
    add_currency(session, 1, 7, "AMD", scale=100, end=date(2021, 7, 8))
    add_currency(session, 2, 7, "AMD", scale=1000, start=date(2021, 7, 9))
    add_rate(session, 1, date(2021, 7, 8), 0.80)  # 0.80 за 100 = 8.0 за 1000
    add_rate(session, 2, date(2021, 7, 9), 8.20)
    session.commit()

    (row,) = collect_report_rows(session, date(2021, 7, 9))

    assert row.scale == 1000
    assert row.change == pytest.approx(0.20)


def test_first_ever_rate_has_no_change(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_rate(session, 431, date(2026, 10, 22), 2.97)
    session.commit()

    (row,) = collect_report_rows(session, REPORT_DATE)

    assert row.change is None


def test_closed_currency_and_currency_without_rates_are_not_reported(seeded: Session) -> None:
    add_currency(seeded, 200, 185, "DEM", end=date(2003, 1, 1))
    add_rate(seeded, 200, date(2002, 12, 31), 1.0)
    add_currency(seeded, 451, 19, "EUR")  # действует, но курсов пока нет
    seeded.commit()

    assert [row.code for row in collect_report_rows(seeded, REPORT_DATE)] == ["USD"]


def test_every_currency_appears_exactly_once_sorted_by_code(session: Session) -> None:
    for cur_id, (parent, code) in enumerate([(19, "EUR"), (145, "USD"), (2, "AUD")], start=10):
        add_currency(session, cur_id, parent, code)
        add_rate(session, cur_id, date(2026, 10, 21), 1.0)
        add_rate(session, cur_id, date(2026, 10, 22), 1.1)
    session.commit()

    assert [row.code for row in collect_report_rows(session, REPORT_DATE)] == ["AUD", "EUR", "USD"]


def test_empty_database_gives_empty_report(session: Session) -> None:
    assert collect_report_rows(session, REPORT_DATE) == []


def test_change_has_no_float_noise(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_rate(session, 431, date(2026, 10, 21), 8.1370)
    add_rate(session, 431, date(2026, 10, 22), 8.0907)
    session.commit()

    (row,) = collect_report_rows(session, REPORT_DATE)

    assert row.change == -0.0463  # а не -0.04630000000000045
