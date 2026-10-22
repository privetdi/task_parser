"""Зависимости задач: настройки, база и фабрики внешних клиентов.

Задачи берут клиентов отсюда, а не создают сами — в тестах фабрики подменяются.
"""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from app.config import Settings
from app.db import SessionFactory
from app.services.mailer import Mailer, build_mailer
from app.sources.iea import ChartsSource, IeaClient
from app.sources.nbrb import NbrbClient


@dataclass
class Dependencies:
    settings: Settings
    session_factory: SessionFactory
    nbrb_client: Callable[[], NbrbClient]
    mailer: Callable[[], Mailer]
    # Клиент IEA открывает браузер, поэтому отдаётся как контекстный менеджер
    iea_client: Callable[[], AbstractContextManager[ChartsSource]]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Раннер подставляет сюда флаг «сервис останавливается»; долгие задачи проверяют его
    # между порциями работы и завершаются, сохранив позицию
    should_stop: Callable[[], bool] = field(default=lambda: False)

    def now(self) -> datetime:
        """Текущее время (UTC, с поясом)."""
        return self.clock()

    def now_local(self) -> datetime:
        """Текущее время в часовом поясе из настроек (по умолчанию Europe/Minsk)."""
        return self.clock().astimezone(self.settings.tz)

    def today(self) -> date:
        return self.now_local().date()


def default_dependencies(settings: Settings, session_factory: SessionFactory) -> Dependencies:
    return Dependencies(
        settings=settings,
        session_factory=session_factory,
        nbrb_client=lambda: NbrbClient(request_delay=settings.nbrb_request_delay),
        mailer=lambda: build_mailer(settings),
        iea_client=lambda: IeaClient(
            profile_dir=settings.iea_profile,
            channel=settings.iea_browser_channel,
            headless=settings.iea_headless,
        ),
    )
