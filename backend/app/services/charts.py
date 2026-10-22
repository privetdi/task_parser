"""Графики IEA: обнаружение новых, скачивание и отправка по одному письму на график."""
import mimetypes
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chart, ChartStatus, DeliveryKind, DeliveryStatus, JobStatus, utcnow
from app.services.deliveries import Outcome, deliver_once, get_delivery
from app.services.mailer import Attachment, Mailer, OutgoingMail
from app.services.result import JobResult
from app.sources.iea import CATALOG_URL, ChartRef, ChartsSource, IeaError


def run_iea_charts(
    session: Session,
    source: ChartsSource,
    mailer: Mailer,
    *,
    charts_dir: Path,
    now: Callable[[], datetime] = utcnow,
    should_stop: Callable[[], bool] = lambda: False,
) -> JobResult:
    """Проверяет первую страницу каталога и отправляет графики, которые ещё не уходили.

    Графики различаются по `slug`. Неотправленными остаются новые и те, по которым прошлый
    запуск завершился ошибкой (скачивания или отправки) — они повторяются. Ошибка по одному
    графику не мешает остальным. Если сайт недоступен или отдал защитную страницу, запуск
    завершается ошибкой с понятной причиной и ничего не отправляет.
    """
    try:
        catalog = source.fetch_catalog()
    except IeaError as exc:
        return JobResult(JobStatus.ERROR, f"Каталог IEA недоступен: {exc}", [str(exc)])

    known = set(session.scalars(select(Chart.slug)))
    new_count = sum(ref.slug not in known for ref in catalog)
    sent = 0
    errors: list[str] = []
    stopped = False

    for ref in catalog:
        if should_stop():  # неотправленные графики подхватит следующий запуск
            stopped = True
            break
        chart = _upsert_chart(session, ref)
        if _already_sent(session, chart):
            continue
        try:
            error = _process_chart(session, source, mailer, chart, charts_dir, now)
        except OSError as exc:  # диск: не записался или не прочитался файл
            error = f"{chart.title}: {exc}"
        if error:
            errors.append(error)
        else:
            sent += 1

    if not errors and sent == 0 and not stopped:
        return JobResult(JobStatus.SUCCESS, f"Новых графиков нет (на странице: {len(catalog)})")
    summary = f"Графиков на странице: {len(catalog)}; новых: {new_count}; отправлено: {sent}"
    if stopped:
        return JobResult(
            JobStatus.INTERRUPTED, f"Остановлено вместе с сервисом. {summary}", errors
        )
    if errors:
        summary += f"; ошибок: {len(errors)}"
        return JobResult(JobStatus.PARTIAL if sent else JobStatus.ERROR, summary, errors)
    return JobResult(JobStatus.SUCCESS, summary)


def _upsert_chart(session: Session, ref: ChartRef) -> Chart:
    chart = session.scalar(select(Chart).where(Chart.slug == ref.slug))
    if chart is None:
        chart = Chart(
            slug=ref.slug, title=ref.title, url=ref.url, status=ChartStatus.DISCOVERED
        )
        session.add(chart)
    else:
        chart.title, chart.url = ref.title, ref.url
    session.commit()
    return chart


def _already_sent(session: Session, chart: Chart) -> bool:
    delivery = get_delivery(session, DeliveryKind.IEA_CHART, chart.slug)
    return delivery is not None and delivery.status == DeliveryStatus.SENT


def _process_chart(
    session: Session,
    source: ChartsSource,
    mailer: Mailer,
    chart: Chart,
    charts_dir: Path,
    now: Callable[[], datetime],
) -> str | None:
    """Скачивает (если нужно) и отправляет один график. Возвращает текст ошибки или None."""
    has_file = chart.image_path and (charts_dir / chart.image_path).is_file()
    if not has_file:
        try:
            path = source.download_image(ChartRef(chart.slug, chart.title, chart.url), charts_dir)
        except IeaError as exc:
            chart.status, chart.error = ChartStatus.DOWNLOAD_ERROR, str(exc)
            session.commit()
            return f"{chart.title}: не удалось скачать изображение: {exc}"
        chart.image_path = str(path.relative_to(charts_dir))
        chart.status, chart.error = ChartStatus.DOWNLOADED, None
        session.commit()

    outcome = deliver_once(
        session,
        mailer,
        DeliveryKind.IEA_CHART,
        chart.slug,
        lambda: _compose_mail(chart, charts_dir / chart.image_path),
        now,
    )
    if outcome.outcome == Outcome.FAILED:
        return f"{chart.title}: письмо не отправлено: {outcome.detail}"
    return None


def _compose_mail(chart: Chart, image: Path) -> OutgoingMail:
    mime_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
    return OutgoingMail(
        subject=f"График IEA: {chart.title}",
        body=(
            f"{chart.title}\n"
            f"Страница графика: {chart.url}\n"
            f"Каталог: {CATALOG_URL}\n\n"
            "Изображение графика — во вложении."
        ),
        attachments=(Attachment(image.name, image.read_bytes(), mime_type),),
    )
