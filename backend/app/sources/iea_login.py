"""Помощник: открывает IEA в окне браузера с профилем сервиса, чтобы владелец один раз прошёл
проверку сайта («Verify you are human») сам.

Запуск, в каталоге backend/:  uv run python -m app.sources.iea_login

Допуск сохраняется в профиле браузера и действует на следующие запуски сервиса, пока сайт не
потребует проверку снова. Сервис на это время лучше остановить: один профиль нельзя открыть
двумя окнами. Проверку за владельца помощник не проходит — окно закрывает тоже владелец.
"""
import sys
import time
from collections.abc import Callable

from playwright.sync_api import BrowserContext, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import get_settings
from app.sources.iea import CATALOG_URL, is_challenge_page, launch_context


def wait_for_manual_check(
    context: BrowserContext,
    *,
    poll_s: float = 1.0,
    deadline_s: float | None = None,
    say: Callable[[str], None] = print,
) -> bool:
    """Ждёт, пока владелец закроет окно (или истечёт `deadline_s`).

    Как только вместо защитной страницы открывается каталог с графиками, сообщает об этом.
    Возвращает True, если каталог открылся.
    """
    started = time.monotonic()
    passed = False
    while context.pages:
        if deadline_s is not None and time.monotonic() - started >= deadline_s:
            break
        try:
            page = context.pages[0]
            title, html = page.title(), page.content()
        except (PlaywrightError, IndexError):
            # Окно закрыли или страница как раз переходит на другую — смотрим ещё раз
            time.sleep(poll_s)
            continue
        if not passed and "data-chart-identifier" in html and not is_challenge_page(title, html):
            passed = True
            say("✓ Каталог открылся — проверка пройдена. Можно закрыть окно браузера.")
        time.sleep(poll_s)
    return passed


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # сообщения должны появляться сразу
    settings = get_settings()
    profile = settings.iea_profile
    print(f"Открываю {CATALOG_URL} в окне браузера.")
    print(f"Профиль браузера: {profile.resolve()}")
    print("Если сайт попросит подтвердить, что вы не робот, — пройдите проверку сами.")
    print("Когда появится каталог графиков, закройте окно. Сервис на это время лучше остановить.")
    with sync_playwright() as playwright:
        try:
            context = launch_context(
                playwright,
                profile_dir=profile,
                channel=settings.iea_browser_channel,
                headless=False,
            )
        except (PlaywrightError, OSError) as exc:
            print(
                f"Не удалось открыть браузер: {exc}\n"
                "Установлен ли он (`playwright install chromium`)? Не открыт ли этот профиль "
                "в другом окне (например, запущенным сервисом)?",
                file=sys.stderr,
            )
            return 1
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(CATALOG_URL, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightError as exc:
                print(f"Страница сразу не открылась ({exc}); обновите её в окне браузера.")
            passed = wait_for_manual_check(context)
        except KeyboardInterrupt:
            print("\nПрервано.")
            passed = False
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass  # окно уже закрыто
    if passed:
        print("Готово: допуск сохранён в профиле. Запускайте сервис как обычно.")
        print("Для регулярных запусков с этим профилем лучше IEA_HEADLESS=false (см. README).")
        return 0
    print("Каталог не открылся — допуск не получен. Запустите помощник ещё раз.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
