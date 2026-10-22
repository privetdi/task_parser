"""Модели базы данных (5 таблиц)."""
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """Время хранится в SQLite как UTC без пояса, а возвращается с поясом UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("ожидается datetime с часовым поясом")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


class JobTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    STARTUP = "startup"


class ChartStatus(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    DOWNLOAD_ERROR = "download_error"


class DeliveryKind(StrEnum):
    RATES_REPORT = "rates_report"
    IEA_CHART = "iea_chart"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


def enum_column(enum_cls: type[StrEnum]) -> Enum:
    """В базе храним значения («success»), а не имена членов («SUCCESS»)."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=16,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class Base(DeclarativeBase):
    pass


class Currency(Base):
    """Одна запись справочника НБ РБ. У валюты может быть несколько версий (Cur_ID)."""

    __tablename__ = "currencies"

    # Cur_ID из источника; одна и та же валюта может иметь разные Cur_ID в разные годы
    cur_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    # Cur_ParentID: общий для всех версий одной валюты
    parent_id: Mapped[int] = mapped_column(index=True)
    abbreviation: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(128))
    # Курс указан за `scale` единиц валюты
    scale: Mapped[int]
    # Как в источнике: 0 — курс ежедневный, 1 — ежемесячный
    periodicity: Mapped[int]
    date_start: Mapped[date]
    date_end: Mapped[date]
    # Позиция догрузки истории: с этой даты и до конца срока версии данные уже загружены
    history_loaded_from: Mapped[date | None]


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    # Один курс на пару (валюта, дата) — защита от дублей на уровне базы
    __table_args__ = (UniqueConstraint("currency_id", "rate_date", name="uq_rate_currency_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    currency_id: Mapped[int] = mapped_column(ForeignKey("currencies.cur_id"))
    rate_date: Mapped[date]
    official_rate: Mapped[float]
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class JobRun(Base):
    """Журнал запусков задач."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[JobTrigger] = mapped_column(enum_column(JobTrigger))
    status: Mapped[JobStatus] = mapped_column(enum_column(JobStatus))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)


class Chart(Base):
    """График IEA, найденный в каталоге."""

    __tablename__ = "charts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Путь страницы графика — стабильный идентификатор для дедупликации
    slug: Mapped[str] = mapped_column(String(256), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024))
    image_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[ChartStatus] = mapped_column(enum_column(ChartStatus))
    error: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Delivery(Base):
    """Учёт отправленного: что уже ушло по почте, а что ждёт повтора."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_delivery_kind_key"),
        Index("ix_delivery_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[DeliveryKind] = mapped_column(enum_column(DeliveryKind))
    # Для отчёта — дата отчёта (YYYY-MM-DD), для графика — slug
    key: Mapped[str] = mapped_column(String(256))
    status: Mapped[DeliveryStatus] = mapped_column(enum_column(DeliveryStatus))
    # Число неудачных попыток отправки
    failed_attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    recipients: Mapped[str | None] = mapped_column(Text)
