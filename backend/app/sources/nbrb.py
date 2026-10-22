"""Клиент публичного API курсов НБ РБ (https://api.nbrb.by/exrates).

Что выяснено проверкой живого API (фрагменты ответов лежат в tests/fixtures/):

* `dynamics/{Cur_ID}` отдаёт не больше 365 точек и **молча обрезает** остальное: запрос
  на два года вернул ровно 365 дней без ошибки. Поэтому окно запроса ограничиваем сами.
* У одной валюты в справочнике несколько записей (`Cur_ID`) с общим `Cur_ParentID`:
  у USD это 145 (до 2021-07-08) и 431 (с 2021-07-09). История лежит под каждым `Cur_ID`
  только в пределах его срока действия.
* Для ежемесячных валют (`Cur_Periodicity=1`) `dynamics` возвращает только даты, когда курс
  устанавливали (по одной точке в месяц), а не ежедневный ряд.
* Дата «завтра» не вызывает ошибку: курс на неё просто пока не опубликован, и точки нет.
* Несуществующий `Cur_ID` даёт `200 []`, а не ошибку, — пустой ответ сам по себе не сбой.
* С 2016-07-01 (деноминация) значения в новых рублях: USD 2016-07-01 = 2.0053.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import httpx

BASE_URL = "https://api.nbrb.by/exrates/"
# Максимальная длина окна запроса dynamics (дней, включая обе границы)
MAX_DYNAMICS_DAYS = 365


class NbrbError(Exception):
    """Запрос к API НБ РБ не удался (в тексте — что именно запрашивали)."""


@dataclass(frozen=True)
class CurrencyInfo:
    """Запись справочника валют. Одна валюта может состоять из нескольких записей."""

    cur_id: int
    parent_id: int
    abbreviation: str
    name: str
    scale: int
    periodicity: int  # 0 — ежедневно, 1 — ежемесячно
    date_start: date
    date_end: date


@dataclass(frozen=True)
class RatePoint:
    rate_date: date
    official_rate: float


def _parse_date(value: str) -> date:
    # Источник отдаёт «2026-10-22T00:00:00»; время всегда нулевое
    return date.fromisoformat(value[:10])


class NbrbClient:
    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        retries: int = 3,
        retry_delay: float = 1.0,
        request_delay: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(
            base_url=BASE_URL,
            timeout=30.0,
            headers={"User-Agent": "rates-service/0.1"},
        )
        self._retries = retries
        self._retry_delay = retry_delay
        self._request_delay = request_delay
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def currencies(self) -> list[CurrencyInfo]:
        """Весь справочник: и действующие валюты, и закрытые, со всеми версиями."""
        items = self._get_json("currencies", what="справочник валют")
        return [
            CurrencyInfo(
                cur_id=item["Cur_ID"],
                parent_id=item["Cur_ParentID"],
                abbreviation=item["Cur_Abbreviation"],
                name=item["Cur_Name"],
                scale=item["Cur_Scale"],
                periodicity=item["Cur_Periodicity"],
                date_start=_parse_date(item["Cur_DateStart"]),
                date_end=_parse_date(item["Cur_DateEnd"]),
            )
            for item in items
        ]

    def dynamics(self, cur_id: int, start: date, end: date) -> list[RatePoint]:
        """Курсы одной версии валюты за окно [start, end] (не длиннее 365 дней)."""
        days = (end - start).days + 1
        if days < 1:
            raise ValueError(f"конец периода {end} раньше начала {start}")
        if days > MAX_DYNAMICS_DAYS:
            # Источник не сообщает об обрезке, поэтому длинное окно — наша ошибка
            raise ValueError(
                f"окно {start}..{end} — {days} дн., источник отдаёт не больше "
                f"{MAX_DYNAMICS_DAYS}; разбейте период на порции"
            )
        items = self._get_json(
            f"rates/dynamics/{cur_id}",
            params={"startdate": start.isoformat(), "enddate": end.isoformat()},
            what=f"курсы Cur_ID={cur_id} за {start}..{end}",
        )
        try:
            return [
                RatePoint(_parse_date(item["Date"]), float(item["Cur_OfficialRate"]))
                for item in items
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise NbrbError(f"курсы Cur_ID={cur_id}: неожиданный формат ответа ({exc!r})") from exc

    def _get_json(
        self, path: str, *, what: str, params: dict[str, str] | None = None
    ) -> list[dict]:
        """GET с паузой между запросами и повторами при сбоях сети и ответах 5xx/429."""
        last_error = "неизвестная ошибка"
        for attempt in range(1, self._retries + 1):
            if self._request_delay:
                self._sleep(self._request_delay)
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:  # сеть, таймаут, обрыв соединения
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                elif response.is_error:
                    # 4xx (кроме 429) повторять бессмысленно
                    raise NbrbError(f"{what}: HTTP {response.status_code}")
                else:
                    data = response.json()
                    if not isinstance(data, list):
                        raise NbrbError(f"{what}: ожидался список, получено {type(data).__name__}")
                    return data
            if attempt < self._retries:
                self._sleep(self._retry_delay * attempt)
        raise NbrbError(f"{what}: {last_error} (попыток: {self._retries})")
