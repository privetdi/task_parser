from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db import SessionFactory, init_db, make_engine, make_session_factory


@pytest.fixture
def engine() -> Engine:
    """Чистая SQLite-база в памяти для каждого теста."""
    engine = make_engine("sqlite://")
    init_db(engine)
    return engine


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory: SessionFactory) -> Iterator[Session]:
    with session_factory() as session:
        yield session
