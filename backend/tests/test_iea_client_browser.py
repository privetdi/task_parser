"""IeaClient в настоящем Chromium, но без сети: ответы «сайта» подменяются через route.

Страница каталога в тестах — либо настоящий фрагмент (tests/fixtures), либо локальная страница
с карточкой графика, которая рисует SVG лениво, когда карточка попадает в область видимости
(как Highcharts на iea.org). Тесты пропускаются, если Chromium не установлен
(`uv run playwright install chromium`).
"""
import time
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Route, sync_playwright

from app.sources.iea import (
    CATALOG_URL,
    ChartRef,
    IeaClient,
    IeaError,
    ImageDownloadError,
    SiteBlockedError,
)
from tests.helpers import FIXTURES

CHALLENGE = (FIXTURES / "iea_cloudflare_challenge.html").read_text(encoding="utf-8")
CATALOG = (FIXTURES / "iea_catalog_cards.html").read_text(encoding="utf-8")
CHART = ChartRef("some-chart", "Some chart", f"{CATALOG_URL}/some-chart")
OTHER = ChartRef("other-chart", "Other chart", f"{CATALOG_URL}/other-chart")


def _card(slug: str, *, renders: bool = True) -> str:
    """Карточка 400×200: SVG появляется, когда карточку прокрутили в область видимости."""
    # Функция-обёртка: у нескольких карточек на странице не должно быть общей области имён
    script = (
        "(() => { const box = document.currentScript.previousElementSibling;"
        "new IntersectionObserver((entries, observer) => {"
        "  for (const entry of entries) { if (!entry.isIntersecting) continue;"
        "    setTimeout(() => { box.innerHTML = '<div class=\"highcharts-container\">"
        '<svg width="400" height="200"><rect width="400" height="200" fill="#2a6fdb"/>'
        "</svg></div>'; }, 200);"
        "    observer.unobserve(entry.target); } }).observe(box); })();"
    )
    return (
        f'<article class="m-chart-listing"><div data-chart-identifier="{slug}">'
        f"<header><h3>{slug}</h3></header>"
        '<div data-chart-container style="width:400px;height:200px">загрузка…</div>'
        f"{'<script>' + script + '</script>' if renders else ''}"
        "</div></article>"
    )


def page_with(*cards: str) -> str:
    # Распорка заставляет прокручивать страницу, чтобы карточка стала видимой
    spacer = '<div style="height:3000px"></div>'
    return f"<html><body>{spacer.join(cards)}</body></html>"


def _chromium_available() -> bool:
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium для Playwright не установлен"
)

Responses = dict[str, tuple[int, str, bytes | str]]


class Site:
    """«Сайт»: отвечает по таблице {url: (статус, тип, тело)} и считает запросы."""

    def __init__(self, responses: Responses) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def __call__(self, context: BrowserContext) -> None:
        def handler(route: Route) -> None:
            self.requests.append(route.request.url)
            status, content_type, body = self.responses.get(
                route.request.url, (404, "text/plain", "not found")
            )
            route.fulfill(status=status, content_type=content_type, body=body)

        context.route("**/*", handler)


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    return tmp_path / "profile"


def client(profile: Path, site: Site | None = None, **options: object) -> IeaClient:
    defaults: dict[str, object] = {
        "settle_ms": 0,
        "render_settle_ms": 0,
        "timeout_ms": 15_000,
        "render_timeout_ms": 10_000,
    }
    return IeaClient(
        profile_dir=profile, configure_context=site, **(defaults | options)  # type: ignore[arg-type]
    )


def assert_card_size(path: Path) -> None:
    """PNG размером с карточку 400×200. Допуск ±1 px: позиция элемента дробная, при снимке
    границы округляются наружу."""
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert abs(width - 400) <= 1 and abs(height - 200) <= 1, (width, height)


# --- Каталог ----------------------------------------------------------------------------


def test_real_catalog_fragment_is_opened_and_parsed(profile: Path) -> None:
    site = Site({CATALOG_URL: (200, "text/html", CATALOG)})

    with client(profile, site) as iea:
        charts = iea.fetch_catalog()

    assert len(charts) == 3
    assert charts[0].title == "Final investment decisions for new LNG liquefaction capacity"


def test_challenge_page_is_reported_as_blocked_with_a_way_out(profile: Path) -> None:
    site = Site({CATALOG_URL: (403, "text/html", CHALLENGE)})

    with client(profile, site) as iea, pytest.raises(SiteBlockedError) as exc:
        iea.fetch_catalog()

    message = str(exc.value)
    assert "Cloudflare" in message and "не обходим" in message
    assert "app.sources.iea_login" in message and "IEA_HEADLESS=false" in message


def test_http_error_page_is_an_iea_error(profile: Path) -> None:
    site = Site({CATALOG_URL: (500, "text/html", "<html>oops</html>")})

    with client(profile, site) as iea, pytest.raises(IeaError, match="HTTP 500"):
        iea.fetch_catalog()


def test_unusable_profile_directory_is_a_readable_error(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")

    with client(not_a_directory) as iea, pytest.raises(IeaError, match="Не удалось запустить"):
        iea.fetch_catalog()


# --- Снимок графика ---------------------------------------------------------------------


def test_snapshot_of_a_lazily_rendered_chart_is_a_png_of_the_card(
    profile: Path, tmp_path: Path
) -> None:
    site = Site({CATALOG_URL: (200, "text/html", page_with(_card("some-chart")))})

    with client(profile, site) as iea:
        iea.fetch_catalog()
        path = iea.download_image(CHART, tmp_path / "charts")

    assert path == tmp_path / "charts" / "some-chart.png"
    assert_card_size(path)
    assert not list((tmp_path / "charts").glob("*.part.png"))


def test_chart_that_never_renders_is_a_download_error_and_leaves_no_file(
    profile: Path, tmp_path: Path
) -> None:
    site = Site({CATALOG_URL: (200, "text/html", page_with(_card("some-chart", renders=False)))})

    with client(profile, site, render_timeout_ms=500) as iea, pytest.raises(
        ImageDownloadError, match="не отрисовался"
    ):
        iea.download_image(CHART, tmp_path / "charts")

    assert list((tmp_path / "charts").iterdir()) == []


def test_chart_missing_from_the_catalog_is_a_download_error(profile: Path, tmp_path: Path) -> None:
    site = Site({CATALOG_URL: (200, "text/html", page_with(_card("some-chart")))})

    with client(profile, site) as iea, pytest.raises(ImageDownloadError, match="нет на странице"):
        iea.download_image(OTHER, tmp_path)


def test_unsafe_identifier_is_rejected_before_touching_the_disk(
    profile: Path, tmp_path: Path
) -> None:
    evil = ChartRef("../escape", "Evil", CATALOG_URL)

    with client(profile) as iea, pytest.raises(ImageDownloadError, match="недопустимый"):
        iea.download_image(evil, tmp_path / "charts")

    assert not (tmp_path / "charts").exists() and not (tmp_path / "escape.png").exists()


def test_catalog_is_requested_once_for_the_catalog_and_all_snapshots(
    profile: Path, tmp_path: Path
) -> None:
    page = page_with(_card("some-chart"), _card("other-chart"))
    site = Site({CATALOG_URL: (200, "text/html", page)})

    with client(profile, site) as iea:
        iea.fetch_catalog()
        iea.download_image(CHART, tmp_path / "charts")
        iea.download_image(OTHER, tmp_path / "charts")

    assert site.requests.count(CATALOG_URL) == 1
    assert {p.name for p in (tmp_path / "charts").iterdir()} == {
        "some-chart.png", "other-chart.png",
    }


def test_snapshot_without_a_prior_catalog_call_opens_the_catalog_itself(
    profile: Path, tmp_path: Path
) -> None:
    site = Site({CATALOG_URL: (200, "text/html", page_with(_card("some-chart")))})

    with client(profile, site) as iea:
        path = iea.download_image(CHART, tmp_path)

    assert_card_size(path)
    assert site.requests.count(CATALOG_URL) == 1


def test_blocked_catalog_blocks_the_snapshot_too(profile: Path, tmp_path: Path) -> None:
    site = Site({CATALOG_URL: (403, "text/html", CHALLENGE)})

    with client(profile, site) as iea, pytest.raises(SiteBlockedError):
        iea.download_image(CHART, tmp_path)


# --- Профиль ----------------------------------------------------------------------------


def test_profile_keeps_the_sites_cookie_between_runs(profile: Path) -> None:
    """Допуск сайта, полученный один раз, должен жить в профиле и после закрытия браузера."""
    site = Site({CATALOG_URL: (200, "text/html", CATALOG)})

    def grant(context: BrowserContext) -> None:
        site(context)
        context.add_cookies([{
            "name": "cf_clearance", "value": "granted", "domain": ".iea.org", "path": "/",
            "expires": time.time() + 3600, "secure": True, "httpOnly": True,
        }])

    first = IeaClient(profile_dir=profile, settle_ms=0, configure_context=grant)
    with first:
        first.fetch_catalog()

    seen: list[str] = []

    def look(context: BrowserContext) -> None:
        site(context)
        seen.extend(f"{c['name']}={c['value']}" for c in context.cookies("https://www.iea.org"))

    second = IeaClient(profile_dir=profile, settle_ms=0, configure_context=look)
    with second:
        second.fetch_catalog()

    assert "cf_clearance=granted" in seen


def test_other_profile_does_not_see_the_cookie(profile: Path, tmp_path: Path) -> None:
    site = Site({CATALOG_URL: (200, "text/html", CATALOG)})

    def grant(context: BrowserContext) -> None:
        site(context)
        context.add_cookies([{
            "name": "cf_clearance", "value": "granted", "domain": ".iea.org", "path": "/",
            "expires": time.time() + 3600, "secure": True,
        }])

    with IeaClient(profile_dir=profile, settle_ms=0, configure_context=grant) as first:
        first.fetch_catalog()

    seen: list[str] = []

    def look(context: BrowserContext) -> None:
        site(context)
        seen.extend(c["name"] for c in context.cookies("https://www.iea.org"))

    with IeaClient(profile_dir=tmp_path / "other", settle_ms=0, configure_context=look) as second:
        second.fetch_catalog()

    assert "cf_clearance" not in seen
