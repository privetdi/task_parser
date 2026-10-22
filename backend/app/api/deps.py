"""Зависимости эндпоинтов FastAPI: всё берётся из состояния приложения."""
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.db import session_scope
from app.jobs.deps import Dependencies
from app.jobs.runner import JobRunner
from app.jobs.scheduler import JobScheduler


def get_deps(request: Request) -> Dependencies:
    return request.app.state.deps


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


def get_scheduler(request: Request) -> JobScheduler:
    return request.app.state.scheduler


def get_session(deps: Annotated[Dependencies, Depends(get_deps)]) -> Iterator[Session]:
    with session_scope(deps.session_factory) as session:
        yield session


# Короткие псевдонимы для сигнатур эндпоинтов
DepsDep = Annotated[Dependencies, Depends(get_deps)]
RunnerDep = Annotated[JobRunner, Depends(get_runner)]
SchedulerDep = Annotated[JobScheduler, Depends(get_scheduler)]
SessionDep = Annotated[Session, Depends(get_session)]
