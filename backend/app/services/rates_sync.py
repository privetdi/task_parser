"""Загрузка курсов НБ РБ в базу: сохранение без дублей, догрузка истории, ежедневная проверка."""
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate, JobStatus, utcnow
from app.services.catalog import active_versions
from app.services.result import JobResult
from app.sources.nbrb import MAX_DYNAMICS_DAYS, NbrbError, RatePoint

# Курсы отдаются с 4 знаками; сравниваем с запасом, чтобы шум float не считался изменением
COMPARE_DIGITS = 6


class RatesSource(Protocol):
    """Что сервису нужно от источника курсов (его реализует NbrbClient)."""

    def dynamics(self, cur_id: int, start: date, end: date) -> list[RatePoint]: ...


@dataclass
class UpsertCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0

    def __iadd__(self, other: "UpsertCounts") -> "UpsertCounts":
        self.added += other.added
        self.updated += other.updated
        self.unchanged += other.unchanged
        return self


def upsert_rates(
    session: Session, currency_id: int, points: list[RatePoint], now: datetime
) -> UpsertCounts:
    """Сохраняет курсы валюты: новые добавляет, изменившиеся обновляет, прочие не трогает.

    Уникальность пары (валюта, дата) гарантирует база; запись идёт одним атомарным
    `INSERT … ON CONFLICT DO UPDATE`, поэтому одновременные запуски задач не конфликтуют.
    """
    counts = UpsertCounts()
    if not points:
        return counts

    # Если источник вернул одну дату дважды — берём последнее значение
    latest = {point.rate_date: point.official_rate for point in points}

    existing = dict(
        session.execute(
            select(ExchangeRate.rate_date, ExchangeRate.official_rate).where(
                ExchangeRate.currency_id == currency_id,
                ExchangeRate.rate_date.between(min(latest), max(latest)),
            )
        ).all()
    )

    changed_rows = []
    for rate_date, rate in latest.items():
        if rate_date not in existing:
            counts.added += 1
        elif round(existing[rate_date], COMPARE_DIGITS) != round(rate, COMPARE_DIGITS):
            counts.updated += 1
        else:
            counts.unchanged += 1
            continue
        changed_rows.append(
            {
                "currency_id": currency_id,
                "rate_date": rate_date,
                "official_rate": rate,
                "updated_at": now,
            }
        )

    if changed_rows:
        statement = sqlite_insert(ExchangeRate)
        statement = statement.on_conflict_do_update(
            index_elements=["currency_id", "rate_date"],
            set_={
                "official_rate": statement.excluded.official_rate,
                "updated_at": statement.excluded.updated_at,
            },
        )
        session.execute(statement, changed_rows)
    return counts


def iter_chunks_newest_first(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Делит период [start, end] на порции не длиннее 365 дней, начиная с самых свежих.

    Порции идут встык, без пропусков и перекрытий: источник молча обрезает длинные окна,
    поэтому любая «дыра» между порциями означала бы потерянные данные.
    """
    chunk_end = end
    while chunk_end >= start:
        chunk_start = max(start, chunk_end - timedelta(days=MAX_DYNAMICS_DAYS - 1))
        yield chunk_start, chunk_end
        chunk_end = chunk_start - timedelta(days=1)


# --- Догрузка истории ---------------------------------------------------------------------


def _window(currency: Currency, today: date, start: date) -> tuple[date, date]:
    """Что нужно загрузить для версии: срок её действия, ограниченный [start, сегодня]."""
    return max(currency.date_start, start), min(currency.date_end, today)


def _relevant_versions(session: Session, today: date, start: date) -> list[Currency]:
    """Версии валют, историю которых нужно загрузить (окно не пустое)."""
    versions = []
    for currency in active_versions(session, today):
        window_start, window_end = _window(currency, today, start)
        if window_start <= window_end:  # версии, закончившиеся раньше границы истории, не нужны
            versions.append(currency)
    return versions


def _is_complete(currency: Currency, today: date, start: date) -> bool:
    window_start, window_end = _window(currency, today, start)
    if window_start > window_end:
        return True  # версия закончилась раньше границы истории — загружать нечего
    return currency.history_loaded_from is not None and currency.history_loaded_from <= window_start


def backfill_history(
    session: Session,
    client: RatesSource,
    *,
    today: date,
    start: date,
    now: Callable[[], datetime] = utcnow,
    should_stop: Callable[[], bool] = lambda: False,
) -> JobResult:
    """Догружает историю курсов порциями от новых к старым и помнит позицию по каждой версии.

    После каждой успешной порции позиция (`history_loaded_from`) сохраняется в базе, поэтому
    после сбоя или перезапуска загрузка продолжается с того же места. Ошибка по одной версии
    валюты не останавливает остальные: она попадает в итог, а позиция этой версии не двигается.
    """
    counts = UpsertCounts()
    chunks = 0
    errors: list[str] = []
    stopped = False

    versions = _relevant_versions(session, today, start)
    for currency in versions:
        if stopped:
            break
        if _is_complete(currency, today, start):
            continue
        window_start, window_end = _window(currency, today, start)
        if currency.history_loaded_from is None:
            range_end = window_end
        else:
            range_end = currency.history_loaded_from - timedelta(days=1)

        for chunk_start, chunk_end in iter_chunks_newest_first(window_start, range_end):
            if should_stop():  # сервис останавливается: позиция уже сохранена, продолжим позже
                stopped = True
                break
            try:
                points = client.dynamics(currency.cur_id, chunk_start, chunk_end)
            except NbrbError as exc:
                errors.append(f"{currency.abbreviation}: {exc}")
                break  # остальные порции этой версии — при следующем запуске
            counts += upsert_rates(session, currency.cur_id, points, now())
            currency.history_loaded_from = chunk_start
            session.commit()
            chunks += 1

    completed = sum(_is_complete(currency, today, start) for currency in versions)
    if not errors and chunks == 0 and not stopped:
        return JobResult(
            JobStatus.SUCCESS, f"История уже загружена полностью (версий валют: {len(versions)})"
        )
    summary = (
        f"Порций загружено: {chunks}; курсы: добавлено {counts.added}, "
        f"изменено {counts.updated}, без изменений {counts.unchanged}; "
        f"версий валют загружено полностью: {completed} из {len(versions)}"
    )
    if stopped:
        return JobResult(
            JobStatus.INTERRUPTED,
            f"Остановлено вместе с сервисом, позиция сохранена. {summary}",
            errors,
        )
    if errors:
        status = JobStatus.PARTIAL if chunks else JobStatus.ERROR
        return JobResult(status, summary, errors)
    return JobResult(JobStatus.SUCCESS, summary)


# --- Ежедневная проверка ------------------------------------------------------------------


def sync_recent_rates(
    session: Session,
    client: RatesSource,
    *,
    today: date,
    lookback_days: int,
    now: Callable[[], datetime] = utcnow,
    should_stop: Callable[[], bool] = lambda: False,
) -> JobResult:
    """Ежедневно запрашивает недавний период и добавляет новые / обновляет изменённые курсы.

    Окно — от «сегодня − lookback_days» до «завтра» (курс на завтра появляется после
    публикации, раньше его просто нет — это не ошибка). Перекрытие с уже загруженным
    нужно, чтобы поймать пересмотренные значения. Если сервис долго не работал, окно
    удлиняется до последней сохранённой даты, чтобы не осталось дыры.
    """
    counts = UpsertCounts()
    checked = 0
    successes = 0
    errors: list[str] = []
    stopped = False

    for currency in _current_versions(session, today):
        if should_stop():
            stopped = True
            break
        start = _sync_start(session, currency, today, lookback_days)
        window_start = max(currency.date_start, start)
        window_end = min(currency.date_end, today + timedelta(days=1))
        checked += 1
        try:
            for chunk_start, chunk_end in iter_chunks_newest_first(window_start, window_end):
                points = client.dynamics(currency.cur_id, chunk_start, chunk_end)
                counts += upsert_rates(session, currency.cur_id, points, now())
                session.commit()
        except NbrbError as exc:
            errors.append(f"{currency.abbreviation}: {exc}")
        else:
            successes += 1

    if counts.added == 0 and counts.updated == 0:
        summary = (
            f"Без изменений: новых и изменённых курсов нет "
            f"(проверено валют: {checked}, значений: {counts.unchanged})"
        )
    else:
        summary = (
            f"Проверено валют: {checked}; курсы: добавлено {counts.added}, "
            f"изменено {counts.updated}, без изменений {counts.unchanged}"
        )
    if stopped:
        return JobResult(
            JobStatus.INTERRUPTED, f"Остановлено вместе с сервисом. {summary}", errors
        )
    if errors:
        summary += f"; ошибок: {len(errors)}"
        return JobResult(JobStatus.PARTIAL if successes else JobStatus.ERROR, summary, errors)
    return JobResult(JobStatus.SUCCESS, summary)


def _current_versions(session: Session, today: date) -> list[Currency]:
    """По одной действующей версии на валюту: ту, что не закончилась (последнюю по началу)."""
    current: dict[int, Currency] = {}
    for currency in active_versions(session, today):  # отсортированы по началу действия
        if currency.date_end >= today:
            current[currency.parent_id] = currency
    return list(current.values())


def _sync_start(session: Session, currency: Currency, today: date, lookback_days: int) -> date:
    start = today - timedelta(days=lookback_days)
    last_stored = session.scalar(
        select(func.max(ExchangeRate.rate_date)).where(ExchangeRate.currency_id == currency.cur_id)
    )
    # Дыра после простоя: последнее сохранённое значение старше окна проверки
    return min(start, last_stored) if last_stored else start


@dataclass(frozen=True)
class BackfillProgress:
    total: int  # версий валют, которые нужно загрузить
    completed: int  # из них загружено полностью
    target_start: date  # граница истории (BACKFILL_START)
    # Данные есть с этой даты по всем валютам; None — пока нечего показать
    loaded_from: date | None

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.completed == self.total


def backfill_progress(session: Session, *, today: date, start: date) -> BackfillProgress:
    versions = _relevant_versions(session, today, start)
    pending = [c for c in versions if not _is_complete(c, today, start)]
    if not versions:
        loaded_from = None
    elif not pending:
        loaded_from = start
    elif any(c.history_loaded_from is None for c in pending):
        loaded_from = None
    else:
        loaded_from = max(c.history_loaded_from for c in pending)
    return BackfillProgress(
        total=len(versions),
        completed=len(versions) - len(pending),
        target_start=start,
        loaded_from=loaded_from,
    )
