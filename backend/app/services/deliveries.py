"""Учёт отправленного: письмо по одному и тому же ключу не уходит дважды."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Delivery, DeliveryKind, DeliveryStatus, utcnow
from app.services.mailer import Mailer, MailError, OutgoingMail


class Outcome(StrEnum):
    SENT = "sent"
    SKIPPED = "skipped"  # уже отправлено раньше, повторно не отправляли
    FAILED = "failed"


@dataclass(frozen=True)
class DeliveryOutcome:
    outcome: Outcome
    detail: str  # пояснение mailer'а при успехе, текст ошибки при сбое


def get_delivery(session: Session, kind: DeliveryKind, key: str) -> Delivery | None:
    return session.scalar(select(Delivery).where(Delivery.kind == kind, Delivery.key == key))


def deliver_once(
    session: Session,
    mailer: Mailer,
    kind: DeliveryKind,
    key: str,
    build_mail: Callable[[], OutgoingMail],
    now: Callable[[], datetime] = utcnow,
) -> DeliveryOutcome:
    """Отправляет письмо, если по (kind, key) оно ещё не ушло успешно.

    `build_mail` вызывается только при необходимости, поэтому отчёт или вложение не
    собираются зря. Неудачная отправка не помечает письмо отправленным — оно будет
    повторено при следующем запуске.
    """
    delivery = get_delivery(session, kind, key)
    if delivery is not None and delivery.status == DeliveryStatus.SENT:
        return DeliveryOutcome(Outcome.SKIPPED, "уже отправлено")
    return _attempt(session, mailer, kind, key, build_mail, now)


def deliver_force(
    session: Session,
    mailer: Mailer,
    kind: DeliveryKind,
    key: str,
    build_mail: Callable[[], OutgoingMail],
    now: Callable[[], datetime] = utcnow,
) -> DeliveryOutcome:
    """Отправляет письмо независимо от того, уходило ли оно раньше (ручной запуск)."""
    return _attempt(session, mailer, kind, key, build_mail, now)


def _attempt(
    session: Session,
    mailer: Mailer,
    kind: DeliveryKind,
    key: str,
    build_mail: Callable[[], OutgoingMail],
    now: Callable[[], datetime],
) -> DeliveryOutcome:
    delivery = get_delivery(session, kind, key)
    if delivery is None:
        delivery = Delivery(kind=kind, key=key, status=DeliveryStatus.PENDING)
        session.add(delivery)
        session.commit()  # «ожидает отправки» видно сразу, даже если процесс упадёт при отправке
    was_sent = delivery.status == DeliveryStatus.SENT

    try:
        detail = mailer.send(build_mail())
    except (MailError, OSError) as exc:  # OSError — например, не прочитался файл вложения
        if not was_sent:
            # Ранее успешно отправленное письмо остаётся «отправленным», даже если повтор не удался
            delivery.status = DeliveryStatus.FAILED
            delivery.failed_attempts += 1
            delivery.last_error = str(exc)
            session.commit()
        return DeliveryOutcome(Outcome.FAILED, str(exc))

    delivery.status = DeliveryStatus.SENT
    delivery.sent_at = now()
    delivery.recipients = ", ".join(mailer.recipients)
    delivery.last_error = None
    session.commit()
    return DeliveryOutcome(Outcome.SENT, detail)
