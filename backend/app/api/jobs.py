"""Задачи: состояние, ручной запуск, журнал запусков."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import RunnerDep, SchedulerDep, SessionDep
from app.api.schemas import JobOut, RunOut, RunStarted
from app.jobs.runner import JobAlreadyRunning, JobRunner, UnknownJob
from app.models import JobRun

router = APIRouter(tags=["jobs"])


def run_out(run: JobRun, runner: JobRunner) -> RunOut:
    job = runner.jobs.get(run.job)
    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    return RunOut(
        id=run.id,
        job=run.job,
        job_title=job.title if job else run.job,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=duration,
        summary=run.summary,
        error=run.error,
    )


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(runner: RunnerDep, scheduler: SchedulerDep, session: SessionDep) -> list[JobOut]:
    result = []
    for job in runner.jobs.values():
        last = session.scalar(
            select(JobRun).where(JobRun.job == job.name).order_by(JobRun.id.desc()).limit(1)
        )
        schedule = scheduler.info(job.name)
        result.append(
            JobOut(
                name=job.name,
                title=job.title,
                description=job.description,
                cron=schedule.cron,
                next_run=schedule.next_run,
                is_running=runner.is_running(job.name),
                last_run=run_out(last, runner) if last else None,
            )
        )
    return result


@router.post("/jobs/{name}/run", status_code=202, response_model=RunStarted)
def run_job(name: str, runner: RunnerDep) -> RunStarted:
    """Запускает задачу вручную в фоне и сразу отвечает; итог смотреть в журнале запусков."""
    try:
        return RunStarted(run_id=runner.start_async(name))
    except UnknownJob:
        raise HTTPException(404, f"Задачи «{name}» нет") from None
    except JobAlreadyRunning:
        raise HTTPException(409, f"Задача «{runner.jobs[name].title}» уже выполняется") from None


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    runner: RunnerDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
    job: str | None = None,
) -> list[RunOut]:
    query = select(JobRun).order_by(JobRun.id.desc()).limit(limit)
    if job:
        query = query.where(JobRun.job == job)
    return [run_out(run, runner) for run in session.scalars(query)]
