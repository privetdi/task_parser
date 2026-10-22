"""Подключение к SQLite и выдача сессий."""
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base

SessionFactory = sessionmaker[Session]


def make_engine(url: str) -> Engine:
    """Создаёт движок SQLite, пригодный для работы из потоков планировщика."""
    database = make_url(url).database
    in_memory = database in (None, "", ":memory:")
    if not in_memory:
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        # Задачи работают в потоках планировщика, а соединение создаётся в другом потоке
        connect_args={"check_same_thread": False},
        # База в памяти (тесты) живёт, пока живо соединение, — берём одно общее
        poolclass=StaticPool if in_memory else None,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        # WAL: чтение из API не блокируется записью задач
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> SessionFactory:
    # expire_on_commit=False: объекты остаются читаемыми после commit
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Создаёт таблицы, если их ещё нет (миграции в проекте не используются)."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """Сессия на одну единицу работы: commit при успехе, rollback при исключении."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
