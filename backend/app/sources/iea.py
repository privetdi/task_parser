"""Источник графиков IEA: каталог https://www.iea.org/data-and-statistics/charts.

Что проверено (2026-10-22, Playwright 1.64, Chromium 156):

* Живой сайт закрыт проверкой Cloudflare. Обычный HTTP-запрос → `403` и страница
  «Just a moment…»; headless- и обычный Chromium получают интерактивную проверку
  «Verify you are human», сами она не проходится. Google Chrome (`IEA_BROWSER_CHANNEL=chrome`)
  на машине разработки не установлен — не проверялся.
* Проверку мы не обходим: не подменяем признаки браузера, не используем решатели и служебные
  адреса. Владелец проходит её один раз вручную (`python -m app.sources.iea_login`), допуск
  сохраняется в профиле браузера и действует на следующие запуски, пока сайт не потребует
  проверку снова. Срок действия допуска задаёт сайт; принимается ли он в headless-режиме —
  неизвестно, поэтому с профилем для регулярных запусков рекомендуется `IEA_HEADLESS=false`.
* Разметка каталога проверена на архивной копии страницы (Wayback Machine, 2026-10-02):
  tests/fixtures/iea_catalog_cards.html — 3 из 12 карточек. Каждый график — элемент с
  `data-chart-identifier="<slug>"` (он же последний сегмент адреса страницы графика),
  заголовок в `<h3>`, ссылка «Open» ведёт на `/data-and-statistics/charts/<slug>`.
* Графики рисуются в браузере (Highcharts) из данных в самой разметке; готового файла
  картинки в каталоге нет, поэтому результат — снимок карточки графика.
* Не проверено на живом сайте: отрисовка Highcharts и снимок карточки (нужен допуск
  владельца). Если разметка окажется иной, менять нужно `parse_catalog` и `download_image`.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

CATALOG_URL = "https://www.iea.org/data-and-statistics/charts"
LOGIN_COMMAND = "uv run python -m app.sources.iea_login"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Слаг становится именем файла и частью селектора, поэтому допускаем только простые символы
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,200}$")


class IeaError(Exception):
    """Не удалось получить данные с IEA (текст рассчитан на человека)."""


class SiteBlockedError(IeaError):
    """Сайт отдал защитную страницу вместо каталога."""


class CatalogParseError(IeaError):
    """Страница открылась, но графиков в ней не нашлось (изменилась вёрстка?)."""


class ImageDownloadError(IeaError):
    """Не удалось получить изображение конкретного графика."""


@dataclass(frozen=True)
class ChartRef:
    slug: str  # стабильный идентификатор графика (data-chart-identifier)
    title: str
    url: str


class ChartsSource(Protocol):
    """Что сервису нужно от источника графиков (его реализует IeaClient)."""

    def fetch_catalog(self) -> list[ChartRef]:
        """Графики первой страницы каталога. Бросает IeaError."""
        ...

    def download_image(self, chart: ChartRef, directory: Path) -> Path:
        """Сохраняет изображение графика в `directory`, возвращает путь к файлу."""
        ...


def is_challenge_page(title: str, html: str) -> bool:
    """Это защитная страница Cloudflare, а не содержимое сайта?

    Признаки берём только те, что бывают у страницы проверки: на обычных страницах IEA
    есть скрипт Turnstile (`challenges.cloudflare.com`), и по нему блокировку не определяем.
    """
    if title.strip().lower().startswith("just a moment"):
        return True
    return "_cf_chl_opt" in html or "challenge-platform" in html


class _CardParser(HTMLParser):
    """Собирает карточки графиков: слаг из `data-chart-identifier`, название из `<h3>`."""

    def __init__(self) -> None:
        super().__init__()
        self.titles: dict[str, str] = {}  # slug -> название ("" — заголовок не найден)
        self.labels: dict[str, str] = {}  # slug -> подпись из data-track-eventlabel (запасная)
        self._slug: str | None = None
        self._heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        identifier = attributes.get("data-chart-identifier")
        if identifier is not None:
            # Новая карточка; с неподходящим слагом её содержимое пропускаем до следующей
            self._slug = identifier if _SLUG.match(identifier) else None
            self._heading = None
            if self._slug:
                self.titles.setdefault(self._slug, "")
            return
        if self._slug is None:
            return
        if tag == "h3" and not self.titles[self._slug] and self._heading is None:
            self._heading = []
        elif tag == "a" and attributes.get("data-track-eventlabel"):
            self.labels.setdefault(self._slug, _clean(attributes["data-track-eventlabel"] or ""))

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._heading is not None and self._slug:
            # Фрагменты текста идут подряд, пробелы между ними уже есть в самом HTML
            self.titles[self._slug] = _clean("".join(self._heading))
            self._heading = None


def _clean(text: str) -> str:
    return " ".join(text.split())


def parse_catalog(html: str) -> list[ChartRef]:
    """Находит в HTML каталога карточки графиков (без повторов, в порядке страницы)."""
    parser = _CardParser()
    parser.feed(html)
    if not parser.titles:
        raise CatalogParseError(
            "На странице каталога не найдено ни одной карточки графика "
            "(data-chart-identifier) — возможно, изменилась вёрстка сайта"
        )
    return [
        ChartRef(
            slug=slug,
            # Заголовок карточки, затем подпись ссылки, затем читаемое имя из слага
            title=title or parser.labels.get(slug) or slug.replace("-", " ").capitalize(),
            url=f"{CATALOG_URL}/{slug}",
        )
        for slug, title in parser.titles.items()
    ]


def launch_context(
    playwright: Playwright, *, profile_dir: Path, channel: str, headless: bool
) -> BrowserContext:
    """Запускает браузер с постоянным профилем: cookie и допуск сайта сохраняются между запусками.

    Один профиль нельзя открыть двумя окнами одновременно (сервис и помощник iea_login).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        channel=None if channel == "chromium" else channel,
        viewport={"width": 1400, "height": 1000},
        locale="en-US",
    )


class IeaClient:
    """Открывает IEA настоящим браузером (Playwright). Использовать как контекстный менеджер:
    один браузер и один заход на страницу каталога на весь запуск задачи."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        channel: str = "chromium",
        headless: bool = True,
        timeout_ms: int = 60_000,
        settle_ms: int = 3_000,
        render_timeout_ms: int = 20_000,
        render_settle_ms: int = 1_000,
        configure_context: Callable[[BrowserContext], None] | None = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._channel = channel
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._settle_ms = settle_ms
        self._render_timeout_ms = render_timeout_ms
        self._render_settle_ms = render_settle_ms
        # Хук для тестов: подменить ответы сайта (context.route) без обращения в сеть
        self._configure_context = configure_context
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        # Страница каталога остаётся открытой: снимки карточек делаются с неё
        self._catalog_page: Page | None = None

    def __enter__(self) -> "IeaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except PlaywrightError:
                pass  # браузер уже закрыт — закрывать нечего
        self._context = self._playwright = self._catalog_page = None

    def fetch_catalog(self) -> list[ChartRef]:
        return parse_catalog(self._open_catalog())

    def download_image(self, chart: ChartRef, directory: Path) -> Path:
        """Снимок карточки графика с открытой страницы каталога (график рисуется в браузере)."""
        if not _SLUG.match(chart.slug):
            raise ImageDownloadError(f"недопустимый идентификатор графика: {chart.slug!r}")
        if self._catalog_page is None:
            self._open_catalog()
        page = self._catalog_page
        assert page is not None

        card = page.locator(f'[data-chart-identifier="{chart.slug}"]')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{chart.slug}.png"
        # Сначала во временный файл: формат Playwright берёт из расширения, поэтому он тоже .png
        partial = directory / f"{chart.slug}.part.png"
        try:
            if card.count() == 0:
                raise ImageDownloadError(
                    "карточки графика нет на странице каталога (график мог уйти с первой страницы)"
                )
            container = card.first.locator("[data-chart-container]")
            container.scroll_into_view_if_needed()  # графики рисуются лениво
            container.locator(".highcharts-container, svg.highcharts-root").first.wait_for(
                state="visible", timeout=self._render_timeout_ms
            )
            page.wait_for_timeout(self._render_settle_ms)  # дать завершиться анимации
            container.screenshot(path=str(partial))
        except PlaywrightTimeoutError as exc:
            partial.unlink(missing_ok=True)
            raise ImageDownloadError(
                f"график не отрисовался за {self._render_timeout_ms // 1000} с"
            ) from exc
        except PlaywrightError as exc:
            partial.unlink(missing_ok=True)
            raise ImageDownloadError(f"не удалось сделать снимок графика: {exc}") from exc

        if not partial.is_file() or not partial.read_bytes().startswith(PNG_SIGNATURE):
            partial.unlink(missing_ok=True)
            raise ImageDownloadError("снимок графика получился пустым или не PNG")
        partial.replace(path)
        return path

    def _open_catalog(self) -> str:
        """Открывает каталог (страница остаётся открытой) и возвращает его HTML."""
        if self._catalog_page is not None:
            self._catalog_page.close()
            self._catalog_page = None
        page = self._new_page()
        try:
            html = self._open(page, CATALOG_URL)
        except Exception:
            page.close()
            raise
        self._catalog_page = page
        return html

    def _new_page(self) -> Page:
        if self._context is None:
            try:
                self._playwright = sync_playwright().start()
                self._context = launch_context(
                    self._playwright,
                    profile_dir=self._profile_dir,
                    channel=self._channel,
                    headless=self._headless,
                )
            except (PlaywrightError, OSError) as exc:  # OSError — профиль не создался
                self.close()
                raise IeaError(
                    f"Не удалось запустить браузер: {exc}. Установлен ли он "
                    "(`playwright install chromium`, для IEA_BROWSER_CHANNEL=chrome — Google "
                    f"Chrome)? Профиль {self._profile_dir} не должен быть открыт в другом окне."
                ) from exc
            if self._configure_context:
                self._configure_context(self._context)
        page = self._context.new_page()
        page.set_default_timeout(self._timeout_ms)
        return page

    def _open(self, page: Page, url: str) -> str:
        """Открывает страницу и возвращает её HTML; защитную страницу сообщает ошибкой."""
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(self._settle_ms)  # дать странице дорисоваться
            title, html = page.title(), page.content()
        except PlaywrightError as exc:
            raise IeaError(f"не удалось открыть {url}: {exc}") from exc
        if is_challenge_page(title, html):
            raise SiteBlockedError(
                "сайт IEA отдал защитную страницу Cloudflare («проверка на робота») вместо "
                "каталога. Автоматически проверку не обходим: пройдите её один раз вручную "
                f"командой `{LOGIN_COMMAND}` в каталоге backend/ и повторите запуск "
                "(для регулярных запусков с профилем лучше IEA_HEADLESS=false)"
            )
        if response is not None and response.status >= 400:
            raise IeaError(f"{url}: HTTP {response.status}")
        return html
