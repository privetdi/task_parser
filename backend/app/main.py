"""Точка входа: сборка приложения FastAPI.

Запуск: `uv run uvicorn app.main:app` (из каталога backend/).
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.jobs.deps import default_dependencies
from app.jobs.registry import JOBS
from app.jobs.runner import JobRunner, mark_interrupted_runs
from app.jobs.scheduler import JobScheduler

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, start_scheduler: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        engine = make_engine(settings.database_url)
        init_db(engine)
        session_factory = make_session_factory(engine)

        deps = default_dependencies(settings, session_factory)
        runner = JobRunner(deps, JOBS)
        scheduler = JobScheduler(runner, settings)
        app.state.deps, app.state.runner, app.state.scheduler = deps, runner, scheduler

        # Запуски, которые остались «выполняется» после прошлой остановки, на самом деле прерваны
        interrupted = mark_interrupted_runs(session_factory, deps.now())
        if interrupted:
            log.warning("Помечено прерванными запусков после остановки сервиса: %d", interrupted)
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()
            runner.shutdown()
            engine.dispose()

    app = FastAPI(title="Курсы НБ РБ и графики IEA", lifespan=lifespan)
    app.include_router(api_router)

    # Собранный интерфейс отдаём тем же сервером; без сборки работает только API
    if settings.frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
    return app


app = create_app()
