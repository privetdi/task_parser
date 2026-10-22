"""Источник IEA: определение защитной страницы и разбор каталога.

Основные проверки идут на НАСТОЯЩИХ страницах: странице блокировки Cloudflare и фрагменте
каталога (архивная копия Wayback, 2026-10-02, 3 из 12 карточек). Граничные случаи — на
минимальных кусках той же структуры карточки (`card()` ниже).
"""
import pytest

from app.sources.iea import (
    CATALOG_URL,
    CatalogParseError,
    ChartRef,
    is_challenge_page,
    parse_catalog,
)
from tests.helpers import FIXTURES

CHALLENGE = (FIXTURES / "iea_cloudflare_challenge.html").read_text(encoding="utf-8")
CATALOG = (FIXTURES / "iea_catalog_cards.html").read_text(encoding="utf-8")

FIRST_SLUG = "final-investment-decisions-for-new-lng-liquefaction-capacity"
FIRST_TITLE = "Final investment decisions for new LNG liquefaction capacity"


def card(slug: str, *, heading: str | None = None, label: str | None = None) -> str:
    """Карточка графика в той же структуре, что на странице каталога, но минимальная."""
    h3 = f'<h3 class="f-ui-2-bold">{heading}</h3>' if heading is not None else ""
    track = f" data-track-eventlabel='{label}'" if label else ""
    return (
        '<article class="m-chart-listing">'
        f'<div class="m-chart-block" data-chart-identifier="{slug}"><header>{h3}'
        f'<a href="/data-and-statistics/charts/{slug}"{track}><span>Open</span>'
        "<svg><title>expand</title></svg></a></header></div></article>"
    )


# --- Защитная страница (настоящая) ------------------------------------------------------


def test_real_cloudflare_challenge_is_recognised() -> None:
    assert is_challenge_page("Just a moment...", CHALLENGE)


def test_challenge_is_recognised_by_content_even_if_title_is_different() -> None:
    assert is_challenge_page("Что-то другое", CHALLENGE)


def test_title_alone_is_enough() -> None:
    assert is_challenge_page("  Just a moment...", "<html></html>")


def test_real_catalog_with_turnstile_script_is_not_a_challenge() -> None:
    # В <head> настоящего каталога есть скрипт Turnstile — по нему блокировку определять нельзя
    assert "challenges.cloudflare.com" in CATALOG
    assert not is_challenge_page("Charts – Data & Statistics - IEA", CATALOG)


def test_page_with_turnstile_widget_is_not_a_challenge() -> None:
    html = '<form><div class="cf-turnstile" data-sitekey="x"></div></form>'

    assert not is_challenge_page("Subscribe", html)


def test_challenge_page_has_no_charts_so_parsing_it_fails_loudly() -> None:
    with pytest.raises(CatalogParseError):
        parse_catalog(CHALLENGE)


# --- Разбор каталога (настоящая разметка) -----------------------------------------------


def test_all_real_cards_are_found_in_page_order() -> None:
    charts = parse_catalog(CATALOG)

    assert [c.slug for c in charts] == [
        FIRST_SLUG,
        "capital-cost-differentials-between-the-top-refining-producer-"
        "and-the-rest-of-the-world-2025",
        "patenting-trends-for-smart-grids-2001-2023",
    ]


def test_title_is_the_card_heading_not_the_button_label() -> None:
    titles = {c.slug: c.title for c in parse_catalog(CATALOG)}

    assert titles[FIRST_SLUG] == FIRST_TITLE
    assert titles["patenting-trends-for-smart-grids-2001-2023"] == (
        "Patenting trends for smart grids, 2001-2023"
    )
    assert not any("Open" in title or "expand" in title for title in titles.values())


def test_url_is_canonical_and_identifier_is_stable_between_calls() -> None:
    first = parse_catalog(CATALOG)
    second = parse_catalog(CATALOG)

    assert first == second
    assert first[0] == ChartRef(FIRST_SLUG, FIRST_TITLE, f"{CATALOG_URL}/{FIRST_SLUG}")


# --- Граничные случаи (минимальная разметка той же структуры) ---------------------------


def test_heading_whitespace_and_nested_tags_are_collapsed() -> None:
    html = card("x", heading="Share of\n   <span>renewables</span>,  2025")

    assert parse_catalog(html)[0].title == "Share of renewables, 2025"


def test_html_entities_in_heading_are_decoded() -> None:
    assert parse_catalog(card("x", heading="Oil &amp; gas"))[0].title == "Oil & gas"


def test_missing_heading_falls_back_to_link_label() -> None:
    html = card("x", label="Global oil inventories")

    assert parse_catalog(html)[0].title == "Global oil inventories"


def test_missing_heading_and_label_fall_back_to_readable_slug() -> None:
    assert parse_catalog(card("some-chart-without-title"))[0].title == "Some chart without title"


def test_second_heading_in_a_card_does_not_replace_the_title() -> None:
    html = card("x", heading="Real title").replace("</header>", "<h3>Other</h3></header>")

    assert parse_catalog(html)[0].title == "Real title"


def test_same_chart_twice_is_one_chart() -> None:
    html = card("x", heading="X") + card("x", heading="X again")

    assert [(c.slug, c.title) for c in parse_catalog(html)] == [("x", "X")]


@pytest.mark.parametrize("slug", ["../../etc/passwd", "a/b", "a b", "", "-start"])
def test_unsafe_identifiers_are_ignored(slug: str) -> None:
    html = card(slug, heading="Evil") + card("good", heading="Good")

    assert [c.slug for c in parse_catalog(html)] == ["good"]


def test_content_of_an_ignored_card_does_not_leak_into_the_next_one() -> None:
    html = card("a/b", heading="Evil") + card("good")

    assert parse_catalog(html)[0].title == "Good"  # из слага, а не «Evil»


def test_page_without_cards_raises_clear_error() -> None:
    with pytest.raises(CatalogParseError, match="не найдено ни одной карточки"):
        parse_catalog("<html><body><a href='/data-and-statistics/charts/x'>X</a></body></html>")
