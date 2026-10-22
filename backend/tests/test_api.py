import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.main import create_app
from app.models import Chart, ChartStatus, JobRun, JobStatus, JobTrigger
from app.sources.nbrb import NbrbError
from tests.fakes import PNG, FakeNbrb, FakeSource, ManagedIea
from tests.helpers import RecordingMailer

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)  # 09:00 в Минске
PASSWORD = "s3cret-пароль"


class Api:
    """Тестовый клиент вместе с приложением и подменёнными внешними системами."""

    def __init__(self, client: TestClient, app: FastAPI) -> None:
        self.http, self.app = client, app
        self.mailer = RecordingMailer()
        self.nbrb = FakeNbrb()
        self.iea = ManagedIea(FakeSource("a", "b"))
        deps = app.state.deps
        deps.nbrb_client = lambda: self.nbrb
        deps.mailer = lambda: self.mailer
        deps.iea_client = self.iea
        deps.clock = lambda: NOW

    def run_and_wait(self, job: str) -> dict:
        """Запускает задачу кнопкой и ждёт её завершения; возвращает запись журнала."""
        response = self.http.post(f"/api/jobs/{job}/run")
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        for _ in range(500):
            run = next(r for r in self.http.get("/api/runs").json() if r["id"] == run_id)
            if run["status"] != "running":
                return run
            threading.Event().wait(0.01)
        raise AssertionError(f"задача {job} не завершилась")


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values = dict(
        data_dir=tmp_path / "data", backfill_on_startup=False, smtp_password=PASSWORD,
        frontend_dist=tmp_path / "нет-сборки", backfill_start=date(2026, 9, 1),
    )
    return Settings(_env_file=None, **{**values, **overrides})


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Api]:
    app = create_app(make_settings(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        yield Api(client, app)


def test_health(api: Api) -> None:
    assert api.http.get("/api/health").json() == {"status": "ok"}


def test_jobs_list_shows_all_jobs_schedule_and_no_runs_yet(api: Api) -> None:
    jobs = {j["name"]: j for j in api.http.get("/api/jobs").json()}

    assert list(jobs) == ["rates_backfill", "rates_sync", "rates_report", "iea_charts"]
    assert jobs["rates_report"]["cron"] == "0 9 * * *"
    assert jobs["rates_backfill"]["cron"] is None
    assert all(j["last_run"] is None and j["is_running"] is False for j in jobs.values())
    assert all(j["title"] and j["description"] for j in jobs.values())


def test_manual_run_returns_202_and_result_appears_in_journal_and_job_state(api: Api) -> None:
    run = api.run_and_wait("rates_backfill")

    assert run["trigger"] == "manual" and run["status"] == "success"
    assert run["job_title"] == "Догрузка истории курсов"
    assert run["duration_seconds"] is not None and run["summary"]
    job = next(j for j in api.http.get("/api/jobs").json() if j["name"] == "rates_backfill")
    assert job["last_run"]["id"] == run["id"]


def test_running_job_refuses_second_manual_start_with_409(api: Api) -> None:
    started, release = threading.Event(), threading.Event()
    original = api.nbrb.currencies

    def slow_currencies():
        started.set()
        release.wait(timeout=5)
        return original()

    api.nbrb.currencies = slow_currencies
    first = api.http.post("/api/jobs/rates_sync/run")
    assert first.status_code == 202 and started.wait(timeout=5)

    second = api.http.post("/api/jobs/rates_sync/run")

    assert second.status_code == 409 and "уже выполняется" in second.json()["detail"]
    job = next(j for j in api.http.get("/api/jobs").json() if j["name"] == "rates_sync")
    assert job["is_running"] is True and job["last_run"]["status"] == "running"
    release.set()
    for _ in range(500):
        if not api.app.state.runner.is_running("rates_sync"):
            break
        threading.Event().wait(0.01)


def test_unknown_job_is_404(api: Api) -> None:
    assert api.http.post("/api/jobs/nope/run").status_code == 404


def test_runs_journal_supports_limit_and_job_filter(api: Api) -> None:
    api.run_and_wait("rates_backfill")
    api.run_and_wait("rates_sync")
    api.run_and_wait("rates_sync")

    assert [r["job"] for r in api.http.get("/api/runs").json()] == [
        "rates_sync", "rates_sync", "rates_backfill",
    ]
    assert len(api.http.get("/api/runs?limit=1").json()) == 1
    assert {r["job"] for r in api.http.get("/api/runs?job=rates_backfill").json()} == {
        "rates_backfill"
    }
    assert api.http.get("/api/runs?limit=0").status_code == 422


def test_failed_run_shows_reason_right_in_the_journal(api: Api) -> None:
    api.nbrb.catalog_error = NbrbError("справочник валют: HTTP 503 (попыток: 3)")

    run = api.run_and_wait("rates_sync")

    assert run["status"] == "error"
    assert "503" in run["error"] and "Справочник валют не загружен" in run["summary"]


def test_latest_rates_and_backfill_progress(api: Api) -> None:
    empty = api.http.get("/api/rates/latest").json()
    assert empty["count"] == 0 and empty["rates"] == []
    before = api.http.get("/api/rates/backfill").json()
    assert before["total"] == 0 and before["is_complete"] is False

    api.run_and_wait("rates_backfill")

    latest = api.http.get("/api/rates/latest").json()
    assert latest["report_date"] == "2026-10-22" and latest["count"] == 2
    usd = next(r for r in latest["rates"] if r["code"] == "USD")
    assert usd["rate_date"] == "2026-10-22" and usd["rate"] == 3.0 and usd["periodicity"] == 0
    progress = api.http.get("/api/rates/backfill").json()
    assert progress["is_complete"] is True and progress["remaining"] == 0
    assert progress["target_start"] == "2026-09-01" and progress["loaded_from"] == "2026-09-01"


def test_excel_download_works_without_sending_anything(api: Api) -> None:
    assert api.http.get("/api/reports/rates.xlsx").status_code == 404  # данных ещё нет
    api.run_and_wait("rates_backfill")

    response = api.http.get("/api/reports/rates.xlsx")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="rates_2026-10-22.xlsx"' in response.headers["content-disposition"]
    sheet = load_workbook(BytesIO(response.content)).active
    assert "22.10.2026" in sheet["A1"].value and sheet["A2"].value == "Валют в отчёте: 2"
    assert api.mailer.sent == [] and api.http.get("/api/deliveries").json() == []


def test_deliveries_show_what_was_sent_to_whom(api: Api) -> None:
    api.run_and_wait("rates_backfill")
    api.run_and_wait("rates_report")

    (delivery,) = api.http.get("/api/deliveries").json()

    assert delivery["kind"] == "rates_report" and delivery["key"] == "2026-10-22"
    assert delivery["status"] == "sent" and delivery["sent_at"]
    assert delivery["recipients"] == "a@example.com, b@example.com"


def test_charts_with_delivery_status_and_images(api: Api) -> None:
    assert api.http.get("/api/charts").json() == []

    run = api.run_and_wait("iea_charts")

    assert run["status"] == "success"
    charts = api.http.get("/api/charts").json()
    assert [c["slug"] for c in charts] == ["b", "a"]  # новые сверху
    assert all(c["delivery_status"] == "sent" and c["sent_at"] for c in charts)
    image = api.http.get(charts[0]["image_url"])
    assert image.status_code == 200 and image.content == PNG


def test_chart_without_image_and_unknown_chart_give_404(api: Api) -> None:
    api.iea.source.broken_images = {"a"}
    api.run_and_wait("iea_charts")

    charts = {c["slug"]: c for c in api.http.get("/api/charts").json()}

    assert charts["a"]["status"] == "download_error" and charts["a"]["image_url"] is None
    assert charts["a"]["delivery_status"] is None  # письмо не ушло, ждёт повтора
    assert api.http.get(f"/api/charts/{charts['a']['id']}/image").status_code == 404
    assert api.http.get("/api/charts/9999/image").status_code == 404


def test_image_path_outside_charts_directory_is_never_served(api: Api, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("секрет")
    with session_scope(api.app.state.deps.session_factory) as session:
        chart = Chart(slug="evil", title="evil", url="u", status=ChartStatus.DOWNLOADED,
                      image_path="../../secret.txt")
        session.add(chart)
        session.flush()
        chart_id = chart.id

    assert api.http.get(f"/api/charts/{chart_id}/image").status_code == 404


def test_config_is_shown_without_secrets(api: Api) -> None:
    response = api.http.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert PASSWORD not in response.text and "smtp_password" not in body
    assert "smtp_user" not in body
    assert body["mail_mode"] == "dry-run" and body["mail_to"] == ["recipient@example.com"]
    assert body["schedules"] == {
        "rates_sync": "0 8 * * *", "rates_report": "0 9 * * *", "iea_charts": "0 10 * * *",
    }


def test_runs_left_running_before_startup_are_marked_interrupted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        session.add(JobRun(job="rates_sync", trigger=JobTrigger.SCHEDULE, status=JobStatus.RUNNING,
                           started_at=NOW))
    engine.dispose()

    with TestClient(create_app(settings, start_scheduler=False)) as client:
        (run,) = client.get("/api/runs").json()

    assert run["status"] == "interrupted" and "остановкой" in run["error"]
    assert run["finished_at"] is not None


def test_state_survives_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, start_scheduler=False)) as client:
        api = Api(client, client.app)
        api.run_and_wait("rates_backfill")
        api.run_and_wait("rates_report")

    with TestClient(create_app(settings, start_scheduler=False)) as client:
        api = Api(client, client.app)
        assert client.get("/api/rates/latest").json()["count"] == 2
        assert len(client.get("/api/deliveries").json()) == 1
        # Запуск отчёта по расписанию после перезапуска не отправит его повторно
        run_id = client.app.state.runner.run_now("rates_report", JobTrigger.SCHEDULE)
        run = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
        assert run["status"] == "skipped" and api.mailer.sent == []


def test_built_frontend_is_served_next_to_the_api(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>Интерфейс</body></html>", encoding="utf-8")
    settings = make_settings(tmp_path, frontend_dist=dist)

    with TestClient(create_app(settings, start_scheduler=False)) as client:
        assert "Интерфейс" in client.get("/").text
        assert client.get("/api/health").json() == {"status": "ok"}


def test_api_works_without_built_frontend(api: Api) -> None:
    assert api.http.get("/").status_code == 404
    assert api.http.get("/api/health").status_code == 200

