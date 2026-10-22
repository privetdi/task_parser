from datetime import date

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Currency
from app.services.catalog import active_versions, sync_catalog
from app.sources.nbrb import BASE_URL, NbrbClient
from tests.helpers import load_fixture

TODAY = date(2026, 10, 22)


def catalog_client() -> NbrbClient:
    catalog = load_fixture("currencies.json")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=catalog))
    return NbrbClient(httpx.Client(base_url=BASE_URL, transport=transport))


def test_versions_of_one_currency_are_stored_under_one_parent(session: Session) -> None:
    sync_catalog(session, catalog_client(), TODAY)

    usd = session.scalars(select(Currency).where(Currency.abbreviation == "USD")).all()
    assert {c.cur_id for c in usd} == {145, 431}
    assert {c.parent_id for c in usd} == {145}


def test_fully_closed_currency_is_not_stored(session: Session) -> None:
    result = sync_catalog(session, catalog_client(), TODAY)

    assert session.get(Currency, 200) is None  # DEM, закрыта в 2003
    # USD, EUR, AZN, ALL — действуют; DEM — нет
    assert result.currencies == 4
    assert result.versions == session.scalar(select(func.count()).select_from(Currency))


def test_old_versions_of_active_currency_are_kept_for_history(session: Session) -> None:
    sync_catalog(session, catalog_client(), TODAY)

    assert session.get(Currency, 292) is not None  # старая версия EUR (до 2021-07-08)
    assert session.get(Currency, 451) is not None  # действующая версия EUR


def test_scale_and_periodicity_are_saved(session: Session) -> None:
    sync_catalog(session, catalog_client(), TODAY)

    assert session.get(Currency, 431).periodicity == 0
    assert session.get(Currency, 507).periodicity == 1


def test_repeated_sync_does_not_duplicate_and_keeps_history_position(session: Session) -> None:
    sync_catalog(session, catalog_client(), TODAY)
    session.get(Currency, 431).history_loaded_from = date(2020, 1, 1)
    session.commit()

    sync_catalog(session, catalog_client(), TODAY)

    assert session.scalar(select(func.count()).select_from(Currency)) == 12
    assert session.get(Currency, 431).history_loaded_from == date(2020, 1, 1)


def test_active_versions_are_grouped_by_currency_and_ordered_by_start(session: Session) -> None:
    sync_catalog(session, catalog_client(), TODAY)

    usd = [c.cur_id for c in active_versions(session, TODAY) if c.parent_id == 145]
    assert usd == [145, 431]


def test_currency_closed_after_sync_is_no_longer_active(session: Session) -> None:
    sync_catalog(session, catalog_client(), TODAY)

    # Через 100 лет у всех версий срок действия закончился (2050-01-01)
    assert active_versions(session, date(2100, 1, 1)) == []
