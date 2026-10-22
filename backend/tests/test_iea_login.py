"""Помощник ручной проверки: ожидание в настоящем headless Chromium с подменой ответов сайта.

Окно браузера в тестах не открывается; `main()` проверяется только на пути ошибки запуска.
"""
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.sources import iea_login
from app.sources.iea import CATALOG_URL, launch_context
from app.sources.iea_login import wait_for_manual_check
from tests.helpers import FIXTURES

CHALLENGE = (FIXTURES / "iea_cloudflare_challenge.html").read_text(encoding="utf-8")
CATALOG = (FIXTURES / "iea_catalog_cards.html").read_text(encoding="utf-8")


def _chromium_available() -> bool:
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Chromium для Playwright не установлен"
)


def open_site(playwright, profile: Path, html: str, status: int = 200) -> BrowserContext:
    """Браузер с профилем, который открыл каталог, а «сайт» ответил `html`."""
    context = launch_context(playwright, profile_dir=profile, channel="chromium", headless=True)

    def handler(route: Route) -> None:
        route.fulfill(status=status, content_type="text/html", body=html)

    context.route("**/*", handler)
    context.pages[0].goto(CATALOG_URL)
    return context


@needs_chromium
def test_helper_reports_when_the_catalog_opens(tmp_path: Path) -> None:
    said: list[str] = []

    with sync_playwright() as playwright:
        context = open_site(playwright, tmp_path / "profile", CATALOG)
        passed = wait_for_manual_check(context, poll_s=0.1, deadline_s=1, say=said.append)
        context.close()

    assert passed is True
    assert len(said) == 1 and "проверка пройдена" in said[0]


@needs_chromium
def test_helper_stays_silent_while_the_challenge_is_shown(tmp_path: Path) -> None:
    said: list[str] = []

    with sync_playwright() as playwright:
        context = open_site(playwright, tmp_path / "profile", CHALLENGE, status=403)
        passed = wait_for_manual_check(context, poll_s=0.1, deadline_s=1, say=said.append)
        context.close()

    assert passed is False and said == []


@needs_chromium
def test_helper_stops_when_the_window_is_closed(tmp_path: Path) -> None:
    said: list[str] = []

    with sync_playwright() as playwright:
        context = open_site(playwright, tmp_path / "profile", CHALLENGE, status=403)
        context.pages[0].close()  # владелец закрыл окно, не пройдя проверку
        passed = wait_for_manual_check(context, poll_s=0.1, deadline_s=5, say=said.append)
        context.close()

    assert passed is False and said == []


def test_main_reports_a_browser_that_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("IEA_PROFILE_DIR", str(tmp_path / "profile"))
    iea_login.get_settings.cache_clear()

    def broken(*args: object, **kwargs: object) -> None:
        raise PlaywrightError("браузер не найден")

    monkeypatch.setattr(iea_login, "launch_context", broken)

    try:
        assert iea_login.main() == 1
    finally:
        iea_login.get_settings.cache_clear()

    captured = capsys.readouterr()
    assert "Не удалось открыть браузер" in captured.err and "браузер не найден" in captured.err
    assert "Профиль браузера:" in captured.out and "profile" in captured.out
