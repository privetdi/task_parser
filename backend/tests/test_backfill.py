from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate, JobStatus
from app.services.rates_sync import (
    backfill_history,
    backfill_progress,
    iter_chunks_newest_first,
)
from app.sources.nbrb import NbrbError, RatePoint

TODAY = date(2026, 10, 22)
START = date(2023, 1, 1)


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


class FakeSource:
    """Источник курсов: одна точка в день, запоминает все запросы, умеет падать."""

    def __init__(self, failing: set[int] | None = None, fail_nth_call: int | None = None) -> None:
        self.calls: list[tuple[int, date, date]] = []
        self.failing = failing or set()
        self.fail_nth_call = fail_nth_call

    def dynamics(self, cur_id: int, start: date, end: date) -> list[RatePoint]:
        self.calls.append((cur_id, start, end))
        if cur_id in self.failing or self.fail_nth_call == len(self.calls):
            raise NbrbError(f"курсы Cur_ID={cur_id} за {start}..{end}: HTTP 500")
        return [RatePoint(d, 3.0) for d in days(start, end)]


def add_currency(
    session: Session, cur_id: int, parent_id: int, abbr: str,
    start: date = date(1991, 1, 1), end: date = date(2050, 1, 1),
) -> Currency:
    currency = Currency(
        cur_id=cur_id, parent_id=parent_id, abbreviation=abbr, name=abbr, scale=1,
        periodicity=0, date_start=start, date_end=end,
    )
    session.add(currency)
    session.commit()
    return currency


def run(session: Session, source: FakeSource, start: date = START):
    return backfill_history(session, source, today=TODAY, start=start)


def rate_count(session: Session, cur_id: int | None = None) -> int:
    query = select(func.count()).select_from(ExchangeRate)
    if cur_id is not None:
        query = query.where(ExchangeRate.currency_id == cur_id)
    return session.scalar(query)


# --- Разбиение на порции ---------------------------------------------------------------


def test_chunks_for_two_years_have_no_gaps_and_no_overlaps() -> None:
    start, end = date(2024, 1, 1), date(2025, 12, 31)  # 731 день
    chunks = list(iter_chunks_newest_first(start, end))

    assert chunks[0][1] == end  # начинаем с самых свежих
    assert chunks[-1][0] == start
    assert all((b - a).days + 1 <= 365 for a, b in chunks)
    # Каждая следующая порция заканчивается ровно за день до начала предыдущей
    for (prev_start, _), (_, next_end) in zip(chunks, chunks[1:], strict=False):
        assert next_end == prev_start - timedelta(days=1)
    assert sum((b - a).days + 1 for a, b in chunks) == 731


def test_period_of_exactly_365_days_is_one_chunk() -> None:
    assert len(list(iter_chunks_newest_first(date(2025, 1, 1), date(2025, 12, 31)))) == 1


def test_period_of_366_days_is_two_chunks() -> None:
    assert len(list(iter_chunks_newest_first(date(2024, 1, 1), date(2024, 12, 31)))) == 2


def test_single_day_and_empty_period() -> None:
    assert list(iter_chunks_newest_first(date(2026, 1, 1), date(2026, 1, 1))) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]
    assert list(iter_chunks_newest_first(date(2026, 1, 2), date(2026, 1, 1))) == []


# --- Догрузка --------------------------------------------------------------------------


def test_long_period_is_loaded_completely_without_losing_days(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    source = FakeSource()

    result = run(session, source)

    expected_days = (TODAY - START).days + 1  # ~1391 день → 4 порции
    assert len(source.calls) == 4
    assert rate_count(session) == expected_days
    assert session.get(Currency, 431).history_loaded_from == START
    assert result.status == JobStatus.SUCCESS and result.errors == []


def test_loading_respects_version_window_and_history_start(session: Session) -> None:
    # USD: старая версия закончилась 2023-06-30, новая началась 2023-07-01
    add_currency(session, 145, 145, "USD", end=date(2023, 6, 30))
    add_currency(session, 431, 145, "USD", start=date(2023, 7, 1))
    source = FakeSource()

    run(session, source)

    old = [(s, e) for cur, s, e in source.calls if cur == 145]
    new = [(s, e) for cur, s, e in source.calls if cur == 431]
    assert min(s for s, _ in old) == START and max(e for _, e in old) == date(2023, 6, 30)
    assert min(s for s, _ in new) == date(2023, 7, 1) and max(e for _, e in new) == TODAY
    # Вместе версии покрывают период без дублей по датам
    assert rate_count(session) == (TODAY - START).days + 1


def test_version_that_ended_before_history_start_is_not_requested(session: Session) -> None:
    add_currency(session, 1, 1, "OLD", end=date(2016, 6, 30))
    source = FakeSource()

    result = run(session, source)

    assert source.calls == []
    assert result.status == JobStatus.SUCCESS


def test_newest_chunk_is_loaded_first(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    source = FakeSource()

    run(session, source)

    assert source.calls[0][2] == TODAY
    assert source.calls[0][1] > source.calls[1][1]


def test_interrupted_load_continues_from_saved_position(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    first = FakeSource(fail_nth_call=2)  # первая порция ок, вторая падает

    result = run(session, first)

    loaded_from = session.get(Currency, 431).history_loaded_from
    assert loaded_from == first.calls[0][1]  # позиция = начало успешной порции
    assert result.status == JobStatus.PARTIAL
    assert rate_count(session) == (TODAY - loaded_from).days + 1

    second = FakeSource()
    result = run(session, second)

    # Повторный запуск не запрашивает уже загруженное: первая порция кончается перед позицией
    assert second.calls[0][2] == loaded_from - timedelta(days=1)
    assert all(end < loaded_from for _, _, end in second.calls)
    assert result.status == JobStatus.SUCCESS
    assert rate_count(session) == (TODAY - START).days + 1


def test_failure_of_one_currency_does_not_stop_others(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_currency(session, 451, 19, "EUR")
    source = FakeSource(failing={431})

    result = run(session, source)

    assert result.status == JobStatus.PARTIAL
    assert rate_count(session, 451) == (TODAY - START).days + 1  # EUR загружена целиком
    assert rate_count(session, 431) == 0
    assert session.get(Currency, 431).history_loaded_from is None  # позиция не сдвинута
    assert len(result.errors) == 1
    # В ошибке видно и валюту, и период
    assert "USD" in result.errors[0] and "Cur_ID=431" in result.errors[0]
    assert str(TODAY) in result.errors[0]


def test_total_failure_is_error_not_partial(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_currency(session, 451, 19, "EUR")

    result = run(session, FakeSource(failing={431, 451}))

    assert result.status == JobStatus.ERROR
    assert len(result.errors) == 2


def test_nothing_to_do_when_history_is_complete(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    run(session, FakeSource())

    source = FakeSource()
    result = run(session, source)

    assert source.calls == []
    assert result.status == JobStatus.SUCCESS
    assert "полностью" in result.summary


def test_moving_history_start_earlier_loads_only_the_older_part(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    run(session, FakeSource())

    earlier = date(2022, 1, 1)
    source = FakeSource()
    run(session, source, start=earlier)

    assert source.calls[0][2] == START - timedelta(days=1)
    assert session.get(Currency, 431).history_loaded_from == earlier
    assert rate_count(session) == (TODAY - earlier).days + 1


def test_repeated_backfill_does_not_create_duplicates(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    run(session, FakeSource())
    before = rate_count(session)

    # Принудительно «забываем» позицию — как если бы базу догружали заново
    session.get(Currency, 431).history_loaded_from = None
    session.commit()
    run(session, FakeSource())

    assert rate_count(session) == before


# --- Прогресс --------------------------------------------------------------------------


def test_progress_before_catalog_is_loaded(session: Session) -> None:
    progress = backfill_progress(session, today=TODAY, start=START)
    assert (progress.total, progress.completed, progress.loaded_from) == (0, 0, None)
    assert not progress.is_complete


def test_progress_after_partial_and_full_load(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    add_currency(session, 451, 19, "EUR")
    assert backfill_progress(session, today=TODAY, start=START).loaded_from is None

    run(session, FakeSource(failing={451}, fail_nth_call=None))
    partial = backfill_progress(session, today=TODAY, start=START)
    assert (partial.total, partial.completed) == (2, 1)
    assert partial.loaded_from is None and not partial.is_complete  # у EUR ничего нет

    run(session, FakeSource())
    done = backfill_progress(session, today=TODAY, start=START)
    assert (done.total, done.completed) == (2, 2)
    assert done.loaded_from == START and done.is_complete


def test_progress_does_not_count_versions_that_are_not_needed(session: Session) -> None:
    add_currency(session, 1, 1, "OLD", end=date(2016, 6, 30))  # закончилась до границы истории
    add_currency(session, 431, 145, "USD")

    progress = backfill_progress(session, today=TODAY, start=START)

    assert (progress.total, progress.completed) == (1, 0)


# --- Остановка сервиса -----------------------------------------------------------------


def test_stop_between_chunks_keeps_position_and_resumes_later(session: Session) -> None:
    add_currency(session, 431, 145, "USD")
    source = FakeSource()

    result = backfill_history(
        session, source, today=TODAY, start=START, should_stop=lambda: len(source.calls) >= 1
    )

    assert result.status == JobStatus.INTERRUPTED
    assert "Остановлено" in result.summary and "позиция сохранена" in result.summary
    assert len(source.calls) == 1
    saved = session.get(Currency, 431).history_loaded_from
    assert saved == source.calls[0][1]

    resumed = FakeSource()
    done = run(session, resumed)

    assert done.status == JobStatus.SUCCESS
    assert resumed.calls[0][2] == saved - timedelta(days=1)  # продолжили, а не начали заново
    assert rate_count(session) == (TODAY - START).days + 1


def test_stop_requested_before_start_does_nothing_and_is_not_reported_as_complete(
    session: Session,
) -> None:
    add_currency(session, 431, 145, "USD")
    source = FakeSource()

    result = backfill_history(
        session, source, today=TODAY, start=START, should_stop=lambda: True
    )

    assert result.status == JobStatus.INTERRUPTED and source.calls == []
    assert session.get(Currency, 431).history_loaded_from is None


def test_job_summary_counts_the_same_versions_as_progress(session: Session) -> None:
    add_currency(session, 1, 1, "OLD", end=date(2016, 6, 30))  # не нужна
    add_currency(session, 431, 145, "USD")

    result = run(session, FakeSource())

    assert "загружено полностью: 1 из 1" in result.summary
