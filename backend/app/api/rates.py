"""Курсы: актуальная таблица, прогресс догрузки, скачивание Excel."""
from fastapi import APIRouter, HTTPException, Response

from app.api.deps import DepsDep, RunnerDep, SessionDep
from app.api.schemas import BackfillOut, LatestRatesOut, RateOut
from app.jobs.scheduler import BACKFILL_JOB
from app.services.mailer import XLSX_MIME
from app.services.rates_sync import backfill_progress
from app.services.report import collect_report_rows, render_report

router = APIRouter(tags=["rates"])


@router.get("/rates/latest", response_model=LatestRatesOut)
def latest_rates(deps: DepsDep, session: SessionDep) -> LatestRatesOut:
    report_date = deps.today()
    rows = collect_report_rows(session, report_date)
    return LatestRatesOut(
        report_date=report_date,
        count=len(rows),
        rates=[
            RateOut(
                code=row.code, name=row.name, scale=row.scale, rate=row.rate,
                rate_date=row.rate_date, periodicity=row.periodicity, change=row.change,
            )
            for row in rows
        ],
    )


@router.get("/rates/backfill", response_model=BackfillOut)
def backfill_state(deps: DepsDep, runner: RunnerDep, session: SessionDep) -> BackfillOut:
    progress = backfill_progress(
        session, today=deps.today(), start=deps.settings.backfill_start
    )
    return BackfillOut(
        total=progress.total,
        completed=progress.completed,
        remaining=progress.total - progress.completed,
        target_start=progress.target_start,
        loaded_from=progress.loaded_from,
        is_complete=progress.is_complete,
        is_running=runner.is_running(BACKFILL_JOB),
    )


@router.get("/reports/rates.xlsx")
def download_report(deps: DepsDep, session: SessionDep) -> Response:
    """Текущий Excel-отчёт без отправки письма."""
    report = render_report(session, deps.today(), deps.now_local())
    if not report.rows:
        raise HTTPException(404, "В базе пока нет курсов — дождитесь загрузки истории")
    return Response(
        content=report.content,
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
