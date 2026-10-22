"""Графики IEA и учёт отправленных писем."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import DepsDep, SessionDep
from app.api.schemas import ChartOut, DeliveryOut
from app.models import Chart, Delivery, DeliveryKind

router = APIRouter(tags=["charts"])


@router.get("/charts", response_model=list[ChartOut])
def list_charts(session: SessionDep) -> list[ChartOut]:
    deliveries = {
        d.key: d
        for d in session.scalars(select(Delivery).where(Delivery.kind == DeliveryKind.IEA_CHART))
    }
    result = []
    for chart in session.scalars(select(Chart).order_by(Chart.id.desc())):
        delivery = deliveries.get(chart.slug)
        result.append(
            ChartOut(
                id=chart.id,
                slug=chart.slug,
                title=chart.title,
                url=chart.url,
                status=chart.status,
                error=chart.error,
                discovered_at=chart.discovered_at,
                image_url=f"/api/charts/{chart.id}/image" if chart.image_path else None,
                delivery_status=delivery.status if delivery else None,
                sent_at=delivery.sent_at if delivery else None,
                failed_attempts=delivery.failed_attempts if delivery else 0,
                last_error=delivery.last_error if delivery else None,
            )
        )
    return result


@router.get("/charts/{chart_id}/image")
def chart_image(chart_id: int, deps: DepsDep, session: SessionDep) -> FileResponse:
    chart = session.get(Chart, chart_id)
    if chart is None or not chart.image_path:
        raise HTTPException(404, "Изображения нет")
    charts_dir = deps.settings.charts_dir.resolve()
    path = (charts_dir / chart.image_path).resolve()
    # Путь хранится относительно каталога графиков; за его пределы не выходим
    if charts_dir not in path.parents or not path.is_file():
        raise HTTPException(404, "Файл изображения не найден")
    return FileResponse(path)


@router.get("/deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[Delivery]:
    """Что отправлено по почте (и кому), а что ждёт повтора из-за ошибки."""
    return list(session.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(limit)))
