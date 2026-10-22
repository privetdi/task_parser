"""Справочник валют: какие валюты действуют и из каких версий они состоят."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Currency
from app.sources.nbrb import NbrbClient


@dataclass(frozen=True)
class CatalogResult:
    currencies: int  # действующих валют (групп версий)
    versions: int  # записей справочника, сохранённых для них


def sync_catalog(session: Session, client: NbrbClient, today: date) -> CatalogResult:
    """Сохраняет действующие валюты со всеми их версиями; закрытые валюты пропускает.

    Валюта действует, если хотя бы одна её версия (общий Cur_ParentID) не закончилась.
    Старые версии нужны, чтобы склеить историю: у USD до 2021-07-09 это Cur_ID=145,
    после — 431.
    """
    infos = client.currencies()
    active_parents = {info.parent_id for info in infos if info.date_end >= today}

    versions = 0
    for info in infos:
        if info.parent_id not in active_parents:
            continue
        row = session.get(Currency, info.cur_id)
        if row is None:
            row = Currency(cur_id=info.cur_id)
            session.add(row)
        row.parent_id = info.parent_id
        row.abbreviation = info.abbreviation
        row.name = info.name
        row.scale = info.scale
        row.periodicity = info.periodicity
        row.date_start = info.date_start
        row.date_end = info.date_end
        # history_loaded_from намеренно не трогаем: это позиция догрузки, а не данные справочника
        versions += 1
    session.flush()
    return CatalogResult(currencies=len(active_parents), versions=versions)


def active_versions(session: Session, today: date) -> list[Currency]:
    """Все версии действующих валют, отсортированные по валюте и началу действия."""
    active_parents = select(Currency.parent_id).where(Currency.date_end >= today)
    return list(
        session.scalars(
            select(Currency)
            .where(Currency.parent_id.in_(active_parents))
            .order_by(Currency.parent_id, Currency.date_start)
        )
    )
