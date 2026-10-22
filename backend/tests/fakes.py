"""Фейки внешних систем, общие для нескольких тестов."""
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from app.sources.iea import ChartRef, ImageDownloadError
from app.sources.nbrb import CurrencyInfo, NbrbError, RatePoint

PNG = b"\x89PNG\r\n fake image"


class FakeNbrb:
    """Справочник из трёх записей (USD в двух версиях и EUR) и курс 3.0 на каждый день.

    Курс на завтра, как и в настоящем API утром, не публикуется: данные заканчиваются на
    `last_day`.
    """

    def __init__(self, last_day: date = date(2026, 10, 22)) -> None:
        self.last_day = last_day
        self.catalog_error: NbrbError | None = None
        self.closed = False

    def currencies(self) -> list[CurrencyInfo]:
        if self.catalog_error:
            raise self.catalog_error
        end = date(2050, 1, 1)
        return [
            CurrencyInfo(145, 145, "USD", "Доллар США", 1, 0, date(1991, 1, 1), date(2025, 12, 31)),
            CurrencyInfo(431, 145, "USD", "Доллар США", 1, 0, date(2026, 1, 1), end),
            CurrencyInfo(451, 19, "EUR", "Евро", 1, 0, date(2021, 7, 9), end),
        ]

    def dynamics(self, cur_id: int, start: date, end: date) -> list[RatePoint]:
        end = min(end, self.last_day)
        return [RatePoint(start + timedelta(days=i), 3.0) for i in range((end - start).days + 1)]

    def close(self) -> None:
        self.closed = True


def ref(slug: str) -> ChartRef:
    return ChartRef(slug, f"График {slug}", f"https://www.iea.org/data-and-statistics/charts/{slug}")


class FakeSource:
    """Каталог IEA в памяти: умеет «блокироваться» и не отдавать картинки по заказу."""

    def __init__(self, *slugs: str) -> None:
        self.catalog = [ref(slug) for slug in slugs]
        self.fetch_error: Exception | None = None
        self.broken_images: set[str] = set()
        self.downloads: list[str] = []

    def fetch_catalog(self) -> list[ChartRef]:
        if self.fetch_error:
            raise self.fetch_error
        return list(self.catalog)

    def download_image(self, chart: ChartRef, directory: Path) -> Path:
        self.downloads.append(chart.slug)
        if chart.slug in self.broken_images:
            raise ImageDownloadError("у графика нет изображения")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{chart.slug}.png"
        path.write_bytes(PNG)
        return path


class ManagedIea:
    """Клиент IEA как контекстный менеджер: запоминает, закрыли ли его."""

    def __init__(self, source: FakeSource) -> None:
        self.source, self.entered, self.exited = source, False, False

    @contextmanager
    def __call__(self):
        self.entered = True
        try:
            yield self.source
        finally:
            self.exited = True
