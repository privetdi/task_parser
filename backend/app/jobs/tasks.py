"""Тела задач: связывают сервисы с настройками и внешними клиентами."""
from sqlalchemy.orm import Session

from app.jobs.deps import Dependencies
from app.models import JobStatus, JobTrigger
from app.services.catalog import sync_catalog
from app.services.charts import run_iea_charts
from app.services.rates_sync import backfill_history, sync_recent_rates
from app.services.report import send_rates_report
from app.services.result import JobResult
from app.sources.nbrb import NbrbError


def rates_backfill(deps: Dependencies, session: Session, trigger: JobTrigger) -> JobResult:
    """Загружает справочник валют и догружает историю курсов с сохранённой позиции."""
    client = deps.nbrb_client()
    try:
        today = deps.today()
        try:
            sync_catalog(session, client, today)
        except NbrbError as exc:
            return _catalog_failed(exc)
        session.commit()
        return backfill_history(
            session, client, today=today, start=deps.settings.backfill_start,
            now=deps.now, should_stop=deps.should_stop,
        )
    finally:
        client.close()


def rates_sync(deps: Dependencies, session: Session, trigger: JobTrigger) -> JobResult:
    """Ежедневная проверка: новые и изменённые курсы за недавний период."""
    client = deps.nbrb_client()
    try:
        today = deps.today()
        try:
            sync_catalog(session, client, today)
        except NbrbError as exc:
            return _catalog_failed(exc)
        session.commit()
        return sync_recent_rates(
            session, client, today=today,
            lookback_days=deps.settings.sync_lookback_days, now=deps.now,
            should_stop=deps.should_stop,
        )
    finally:
        client.close()


def rates_report(deps: Dependencies, session: Session, trigger: JobTrigger) -> JobResult:
    """Excel-отчёт на почту. По расписанию — раз за дату, вручную — всегда."""
    return send_rates_report(
        session,
        deps.mailer(),
        report_date=deps.today(),
        generated_at=deps.now_local(),
        force=trigger == JobTrigger.MANUAL,
        now=deps.now,
    )


def iea_charts(deps: Dependencies, session: Session, trigger: JobTrigger) -> JobResult:
    """Новые графики первой страницы каталога IEA: скачать и отправить по одному письму."""
    with deps.iea_client() as source:
        return run_iea_charts(
            session, source, deps.mailer(), charts_dir=deps.settings.charts_dir,
            now=deps.now, should_stop=deps.should_stop,
        )


def _catalog_failed(exc: NbrbError) -> JobResult:
    return JobResult(JobStatus.ERROR, f"Справочник валют не загружен: {exc}", [str(exc)])
