"""Настройки сервиса для показа в интерфейсе (без секретов)."""
from fastapi import APIRouter

from app.api.deps import DepsDep
from app.api.schemas import ConfigOut
from app.jobs.registry import JOBS

router = APIRouter(tags=["config"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config", response_model=ConfigOut)
def get_config(deps: DepsDep) -> ConfigOut:
    settings = deps.settings
    # Поля перечислены явно: новый секрет в Settings случайно не попадёт в ответ
    return ConfigOut(
        mail_mode=settings.mail_mode,
        mail_from=settings.mail_from,
        mail_to=settings.mail_to,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_security=settings.smtp_security,
        timezone=settings.timezone,
        schedules={
            job.name: getattr(settings, job.cron_setting) for job in JOBS if job.cron_setting
        },
        backfill_start=settings.backfill_start,
        backfill_on_startup=settings.backfill_on_startup,
        sync_lookback_days=settings.sync_lookback_days,
        iea_headless=settings.iea_headless,
    )
