from fastapi import APIRouter

from app.api import charts, config, jobs, rates

router = APIRouter(prefix="/api")
for module in (config, jobs, rates, charts):
    router.include_router(module.router)
