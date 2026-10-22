from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate, JobStatus
from app.services.rates_sync import sync_recent_rates
from app.sources.nbrb import NbrbError, RatePoint

TODAY = date(2026, 10, 22)
LOOKBACK = 30


class ApiLikeSource:
    """Ведёт себя как API: отдаёт только то, что «опубликовано», в пределах окна."""

    def __init__(self, data: dict[int, dict[date, float]], failing: set[int] | None = None) -> None:
        self.data = data
        self.failing = failing or set()
        self.calls: list[tuple[int, date, date]] = []

    def dynamics(self, cur_id: int, start: date, end: date) -> list[RatePoint]:
        self.calls.append((cur_id, start, end))
        if cur_id in self.failing:
            raise NbrbError(f"курсы Cur_ID={cur_id} за {start}..{end}: HTTP 500")
        series = self.data.get(cur_id, {})
        return [RatePoint(d, v) for d, v in sorted(series.items()) if start <= d <= end]


def series(start: date, end: date, value: float = 3.0) -> dict[date, float]:
    return {start + timedelta(days=i): value for i in range((end - start).days + 1)}


def add_currency(
    session: Session, cur_id: int, parent_id: int, abbr: str,
    start: date = date(1991, 1, 1), end: date = date(2050, 1, 1),
) -> None:
    session.add(Currency(
        cur_id=cur_id, parent_id=parent_id, abbreviation=abbr, name=abbr, scale=1,
        periodicity=0, date_start=start, date_end=end,
    ))
    session.commit()


def sync(session: Session, source: ApiLikeSource, today: date = TODAY):
    return sync_recent_rates(session, source, today=today, lookback_days=LOOKBACK)


def rates_of(session: Session, cur_id: int) -> dict[date, float]:
    rows = session.scalars(select(ExchangeRate).where(ExchangeRate.currency_id == cur_id))
    return {r.rate_date: r.official_rate for r in rows}


def test_new_rate_for_new_date_is_added(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    published = series(date(2026, 9, 1), date(2026, 10, 21))
    sync(session, ApiLikeSource({431: published}))

    published[date(2026, 10, 22)] = 3.1  # источник опубликовал курс на новую дату
    result = sync(session, ApiLikeSource({431: published}))

    assert result.status == JobStatus.SUCCESS
    assert "добавлено 1" in result.summary
    assert rates_of(session, 431)[date(2026, 10, 22)] == 3.1


def test_changed_published_rate_is_updated(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    published = series(date(2026, 10, 1), date(2026, 10, 21))
    sync(session, ApiLikeSource({431: published}))

    published[date(2026, 10, 15)] = 3.5  # источник пересмотрел ранее опубликованный курс
    result = sync(session, ApiLikeSource({431: published}))

    assert "изменено 1" in result.summary and "добавлено 0" in result.summary
    assert rates_of(session, 431)[date(2026, 10, 15)] == 3.5


def test_nothing_changed_is_reported_and_database_is_untouched(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    published = series(date(2026, 10, 1), date(2026, 10, 21))
    sync(session, ApiLikeSource({431: published}))
    before = session.scalar(select(func.count()).select_from(ExchangeRate))

    result = sync(session, ApiLikeSource({431: published}))

    assert result.status == JobStatus.SUCCESS
    assert "Без изменений" in result.summary
    assert session.scalar(select(func.count()).select_from(ExchangeRate)) == before


def test_request_window_is_lookback_to_tomorrow_for_current_version_only(session: Session) -> None:
    add_currency(session, 145, 145, "USD", end=date(2021, 7, 8))  # старая версия
    add_currency(session, 431, 145, "USD", start=date(2021, 7, 9))
    source = ApiLikeSource({431: series(date(2026, 9, 1), TODAY)})
    sync(session, source)
    session.expunge_all()
    source.calls.clear()

    sync(session, source)

    assert source.calls == [(431, TODAY - timedelta(days=LOOKBACK), TODAY + timedelta(days=1))]


def test_unpublished_tomorrow_is_not_an_error(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    result = sync(session, ApiLikeSource({431: series(date(2026, 10, 1), TODAY)}))  # на 23.10 нет

    assert result.status == JobStatus.SUCCESS and result.errors == []


def test_gap_after_long_downtime_is_closed(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    last_before_downtime = TODAY - timedelta(days=100)
    sync(session, ApiLikeSource({431: series(date(2026, 1, 1), last_before_downtime)}),
         today=last_before_downtime)

    source = ApiLikeSource({431: series(date(2026, 1, 1), TODAY)})
    sync(session, source)

    # Окно удлинилось до последней сохранённой даты, а между первой сохранённой и
    # сегодняшней датой не осталось ни одного пропущенного дня
    assert source.calls[0][1] == last_before_downtime
    stored = rates_of(session, 431)
    assert min(stored) == last_before_downtime - timedelta(days=LOOKBACK)
    assert stored.keys() == set(series(min(stored), TODAY))


def test_gap_longer_than_a_year_is_loaded_in_chunks(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    old_today = date(2025, 1, 31)
    sync(session, ApiLikeSource({431: series(date(2025, 1, 1), old_today)}), today=old_today)

    source = ApiLikeSource({431: series(date(2025, 1, 1), TODAY)})
    sync(session, source)

    assert len(source.calls) >= 2
    assert all((end - start).days + 1 <= 365 for _, start, end in source.calls)
    assert rates_of(session, 431).keys() == set(series(date(2025, 1, 1), TODAY))


def test_first_run_without_data_uses_lookback_window(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    source = ApiLikeSource({})
    sync(session, source)

    assert source.calls == [(431, TODAY - timedelta(days=LOOKBACK), TODAY + timedelta(days=1))]


def test_window_is_clipped_to_version_start(session: Session) -> None:
    started = TODAY - timedelta(days=5)
    add_currency(session, 431, 145, "USD", start=started)
    source = ApiLikeSource({})
    sync(session, source)

    assert source.calls[0][1] == started


def test_failure_of_one_currency_does_not_stop_others(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_currency(session, 451, 19, "EUR")
    source = ApiLikeSource({451: series(date(2026, 10, 1), TODAY)}, failing={431})

    result = sync(session, source)

    assert result.status == JobStatus.PARTIAL
    assert len(rates_of(session, 451)) == 22
    assert len(result.errors) == 1 and "USD" in result.errors[0]
    assert "ошибок: 1" in result.summary


def test_all_failures_make_the_run_an_error(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    result = sync(session, ApiLikeSource({}, failing={431}))

    assert result.status == JobStatus.ERROR


def test_closed_currency_is_not_requested(session: Session) -> None:
    add_currency(session, 200, 185, "DEM", end=date(2003, 1, 1))
    source = ApiLikeSource({})

    sync(session, source)

    assert source.calls == []


def test_stop_between_currencies_keeps_what_is_already_saved(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_currency(session, 451, 19, "EUR")
    source = ApiLikeSource({
        431: series(date(2026, 10, 1), TODAY), 451: series(date(2026, 10, 1), TODAY),
    })

    result = sync_recent_rates(
        session, source, today=TODAY, lookback_days=LOOKBACK,
        should_stop=lambda: len(source.calls) >= 1,
    )

    assert result.status == JobStatus.INTERRUPTED and "Остановлено" in result.summary
    assert len(source.calls) == 1
    # Валюты идут по parent_id: EUR (19) раньше USD (145)
    assert len(rates_of(session, 451)) == 22 and rates_of(session, 431) == {}
