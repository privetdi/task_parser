"""Настройки сервиса: всё берётся из переменных окружения или файла `.env`.

Список параметров с пояснениями — в `.env.example` в корне репозитория.
"""
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # `.env` ищем и в backend/ (откуда запускается сервис), и в корне репозитория
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Почта ---
    mail_mode: Literal["dry-run", "smtp"] = "dry-run"
    mail_from: str = "rates@localhost"
    # NoDecode: список приходит строкой "a@x.com, b@y.com", а не JSON
    mail_to: Annotated[list[str], NoDecode] = ["recipient@example.com"]
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"

    # --- Расписание ---
    timezone: str = "Europe/Minsk"
    cron_rates_sync: str = "0 8 * * *"
    cron_rates_report: str = "0 9 * * *"
    cron_iea_charts: str = "0 10 * * *"

    # --- Курсы НБ РБ ---
    backfill_start: date = date(2016, 7, 1)
    backfill_on_startup: bool = True
    sync_lookback_days: int = 30
    nbrb_request_delay: float = 0.1

    # --- Графики IEA ---
    iea_headless: bool = True
    # chromium — встроенный в Playwright, chrome — установленный Google Chrome
    iea_browser_channel: Literal["chromium", "chrome"] = "chromium"
    # Профиль браузера (cookie, допуск сайта после ручной проверки); по умолчанию в data_dir
    iea_profile_dir: Path | None = None

    # --- Пути ---
    data_dir: Path = Path("../data")
    # Собранный frontend; если каталога нет, API работает без интерфейса
    frontend_dist: Path = Path("../frontend/dist")

    @field_validator("mail_to", mode="before")
    @classmethod
    def _split_recipients(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"неизвестный часовой пояс «{value}»") from exc
        return value

    @field_validator("cron_rates_sync", "cron_rates_report", "cron_iea_charts")
    @classmethod
    def _check_cron(cls, value: str) -> str:
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"некорректное cron-выражение «{value}»: {exc}") from exc
        return value

    @field_validator("sync_lookback_days")
    @classmethod
    def _check_lookback(cls, value: int) -> int:
        if value < 1:
            raise ValueError("должно быть не меньше 1")
        return value

    @model_validator(mode="after")
    def _check_mail(self) -> "Settings":
        """Не ждём первой отправки, чтобы сообщить, чего не хватает для почты."""
        missing = []
        if not self.mail_to:
            missing.append("MAIL_TO")
        if self.mail_mode == "smtp" and not self.smtp_host:
            missing.append("SMTP_HOST")
        if missing:
            raise ValueError(
                f"для MAIL_MODE={self.mail_mode} не заданы обязательные настройки: "
                + ", ".join(missing)
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'rates.db'}"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def charts_dir(self) -> Path:
        return self.data_dir / "charts"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def iea_profile(self) -> Path:
        return self.iea_profile_dir or self.data_dir / "browser-profile"


@lru_cache
def get_settings() -> Settings:
    return Settings()
