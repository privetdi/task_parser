"""Результат работы задачи: его возвращают сервисы, а раннер записывает в журнал."""
from dataclasses import dataclass, field

from app.models import JobStatus


@dataclass
class JobResult:
    status: JobStatus
    summary: str
    errors: list[str] = field(default_factory=list)
