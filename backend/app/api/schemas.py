"""Формы ответов API."""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models import ChartStatus, DeliveryKind, DeliveryStatus, JobStatus, JobTrigger


class RunOut(BaseModel):
    id: int
    job: str
    job_title: str
    trigger: JobTrigger
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    summary: str
    error: str | None


class JobOut(BaseModel):
    name: str
    title: str
    description: str
    cron: str | None
    next_run: datetime | None
    is_running: bool
    last_run: RunOut | None


class RunStarted(BaseModel):
    run_id: int


class RateOut(BaseModel):
    code: str
    name: str
    scale: int
    rate: float
    rate_date: date
    periodicity: int  # 0 — ежедневно, 1 — ежемесячно
    change: float | None


class LatestRatesOut(BaseModel):
    report_date: date
    count: int
    rates: list[RateOut]


class BackfillOut(BaseModel):
    total: int
    completed: int
    remaining: int
    target_start: date
    loaded_from: date | None
    is_complete: bool
    is_running: bool


class ChartOut(BaseModel):
    id: int
    slug: str
    title: str
    url: str
    status: ChartStatus
    error: str | None
    discovered_at: datetime
    image_url: str | None
    delivery_status: DeliveryStatus | None
    sent_at: datetime | None
    failed_attempts: int
    last_error: str | None


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: DeliveryKind
    key: str
    status: DeliveryStatus
    sent_at: datetime | None
    recipients: str | None
    failed_attempts: int
    last_error: str | None


class ConfigOut(BaseModel):
    """Настройки для показа в интерфейсе. Пароли и прочие секреты сюда не попадают."""

    mail_mode: str
    mail_from: str
    mail_to: list[str]
    smtp_host: str
    smtp_port: int
    smtp_security: str
    timezone: str
    schedules: dict[str, str]
    backfill_start: date
    backfill_on_startup: bool
    sync_lookback_days: int
    iea_headless: bool
