from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionFactory
from app.models import (
    Chart,
    ChartStatus,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    JobStatus,
)
from app.services.charts import run_iea_charts
from app.services.deliveries import get_delivery
from app.services.mailer import MailError
from app.sources.iea import CatalogParseError, ChartRef, SiteBlockedError
from tests.fakes import PNG, FakeSource, ref
from tests.helpers import RecordingMailer


@pytest.fixture
def mailer() -> RecordingMailer:
    return RecordingMailer()


def run(session: Session, source: FakeSource, mailer: RecordingMailer, tmp_path: Path):
    return run_iea_charts(session, source, mailer, charts_dir=tmp_path / "charts")


def delivery_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Delivery))


def test_three_new_charts_are_three_separate_mails_with_images(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b", "c")

    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.SUCCESS
    assert "новых: 3" in result.summary and "отправлено: 3" in result.summary
    assert len(mailer.sent) == 3
    for mail, slug in zip(mailer.sent, "abc", strict=True):
        assert mail.subject == f"График IEA: График {slug}"
        assert f"charts/{slug}" in mail.body
        (attachment,) = mail.attachments
        assert attachment.filename == f"{slug}.png"
        assert attachment.mime_type == "image/png" and attachment.content == PNG
    charts = session.scalars(select(Chart)).all()
    assert {c.status for c in charts} == {ChartStatus.DOWNLOADED}
    assert (tmp_path / "charts" / "a.png").read_bytes() == PNG


def test_second_run_without_new_charts_sends_nothing(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b")
    run(session, source, mailer, tmp_path)

    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.SUCCESS and "Новых графиков нет" in result.summary
    assert len(mailer.sent) == 2
    assert source.downloads == ["a", "b"]  # повторно ничего не скачивали


def test_already_sent_charts_are_not_resent_after_restart(
    session_factory: SessionFactory, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b")
    with session_factory() as before_restart:
        run(before_restart, source, mailer, tmp_path)

    # «Перезапуск»: новая сессия, всё состояние — только в базе
    with session_factory() as after_restart:
        result = run(after_restart, source, mailer, tmp_path)

    assert result.status == JobStatus.SUCCESS and len(mailer.sent) == 2


def test_only_the_new_chart_is_sent_when_catalog_grows(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b")
    run(session, source, mailer, tmp_path)
    source.catalog.append(ref("c"))

    result = run(session, source, mailer, tmp_path)

    assert "новых: 1" in result.summary
    assert [m.subject for m in mailer.sent][2:] == ["График IEA: График c"]


def test_failed_send_of_one_chart_does_not_block_others_and_is_retried_alone(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b", "c")
    original_send = mailer.send

    def flaky_send(mail):
        if "График b" in mail.subject:
            raise MailError("SMTP-сервер недоступен")
        return original_send(mail)

    mailer.send = flaky_send
    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.PARTIAL
    assert len(mailer.sent) == 2 and len(result.errors) == 1
    assert "График b" in result.errors[0] and "недоступен" in result.errors[0]
    failed = get_delivery(session, DeliveryKind.IEA_CHART, "b")
    assert failed.status == DeliveryStatus.FAILED and failed.failed_attempts == 1

    mailer.send = original_send
    retry = run(session, source, mailer, tmp_path)

    assert retry.status == JobStatus.SUCCESS
    assert [m.subject for m in mailer.sent][2:] == ["График IEA: График b"]
    assert get_delivery(session, DeliveryKind.IEA_CHART, "b").status == DeliveryStatus.SENT
    assert source.downloads == ["a", "b", "c"]  # картинку b второй раз не качали


def test_download_error_of_one_chart_is_recorded_and_retried_next_run(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b")
    source.broken_images = {"a"}

    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.PARTIAL
    assert len(mailer.sent) == 1  # b ушёл
    broken = session.scalar(select(Chart).where(Chart.slug == "a"))
    assert broken.status == ChartStatus.DOWNLOAD_ERROR and "нет изображения" in broken.error
    assert get_delivery(session, DeliveryKind.IEA_CHART, "a") is None

    source.broken_images = set()
    run(session, source, mailer, tmp_path)

    assert len(mailer.sent) == 2
    session.refresh(broken)
    assert broken.status == ChartStatus.DOWNLOADED and broken.error is None


def test_blocked_site_is_an_explained_error_and_sends_nothing(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a")
    source.fetch_error = SiteBlockedError("сайт отдал проверку на робота (Cloudflare)")

    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.ERROR
    assert "проверку на робота" in result.summary and result.errors
    assert mailer.sent == [] and delivery_count(session) == 0
    assert session.scalar(select(func.count()).select_from(Chart)) == 0


def test_page_without_charts_is_an_error_not_silence(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource()
    source.fetch_error = CatalogParseError("не найдено ни одной ссылки на график")

    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.ERROR and "ни одной ссылки" in result.summary


def test_deleted_image_file_is_downloaded_again(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a")
    mailer.error = MailError("сбой")  # первая отправка не удалась, файл остался на диске
    run(session, source, mailer, tmp_path)
    (tmp_path / "charts" / "a.png").unlink()
    mailer.error = None

    run(session, source, mailer, tmp_path)

    assert source.downloads == ["a", "a"] and len(mailer.sent) == 1


def test_unreadable_attachment_is_a_per_chart_error(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b")
    source.download_image_original = source.download_image

    def vanishing(chart: ChartRef, directory: Path) -> Path:
        path = source.download_image_original(chart, directory)
        if chart.slug == "a":
            path.unlink()  # файл пропал между скачиванием и отправкой
        return path

    source.download_image = vanishing
    result = run(session, source, mailer, tmp_path)

    assert result.status == JobStatus.PARTIAL and len(mailer.sent) == 1
    assert "График a" in result.errors[0]


def test_title_change_in_catalog_updates_the_stored_title(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a")
    run(session, source, mailer, tmp_path)
    source.catalog = [ChartRef("a", "Новое название", ref("a").url)]

    run(session, source, mailer, tmp_path)

    assert session.scalar(select(Chart.title).where(Chart.slug == "a")) == "Новое название"
    assert len(mailer.sent) == 1  # переименование не повод слать график снова


def test_stop_between_charts_leaves_the_rest_for_the_next_run(
    session: Session, mailer: RecordingMailer, tmp_path: Path
) -> None:
    source = FakeSource("a", "b", "c")

    stopped = run_iea_charts(
        session, source, mailer, charts_dir=tmp_path / "charts",
        should_stop=lambda: len(mailer.sent) >= 1,
    )

    assert stopped.status == JobStatus.INTERRUPTED and "Остановлено" in stopped.summary
    assert len(mailer.sent) == 1

    resumed = run(session, source, mailer, tmp_path)

    assert resumed.status == JobStatus.SUCCESS
    assert [m.subject for m in mailer.sent] == [
        "График IEA: График a", "График IEA: График b", "График IEA: График c",
    ]
