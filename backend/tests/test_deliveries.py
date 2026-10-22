from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Delivery, DeliveryKind, DeliveryStatus
from app.services.deliveries import Outcome, deliver_force, deliver_once, get_delivery
from app.services.mailer import MailError, OutgoingMail
from tests.helpers import RecordingMailer

T1 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
KIND = DeliveryKind.RATES_REPORT


class Builder:
    """Фабрика письма, которая считает, сколько раз её вызывали."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> OutgoingMail:
        self.calls += 1
        return OutgoingMail(subject="Тема", body="Тело")


@pytest.fixture
def mailer() -> RecordingMailer:
    return RecordingMailer()


def delivery_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Delivery))


def test_first_delivery_is_sent_and_recorded(session: Session, mailer: RecordingMailer) -> None:
    build = Builder()

    result = deliver_once(session, mailer, KIND, "2026-10-22", build, lambda: T1)

    assert result.outcome == Outcome.SENT and result.detail == "отправлено через test"
    row = get_delivery(session, KIND, "2026-10-22")
    assert row.status == DeliveryStatus.SENT
    assert row.sent_at == T1
    assert row.recipients == "a@example.com, b@example.com"
    assert row.failed_attempts == 0 and row.last_error is None
    assert len(mailer.sent) == 1


def test_repeated_delivery_is_skipped_and_mail_is_not_even_built(
    session: Session, mailer: RecordingMailer
) -> None:
    build = Builder()
    deliver_once(session, mailer, KIND, "2026-10-22", build, lambda: T1)

    result = deliver_once(session, mailer, KIND, "2026-10-22", build, lambda: T2)

    assert result.outcome == Outcome.SKIPPED
    assert len(mailer.sent) == 1 and build.calls == 1
    assert get_delivery(session, KIND, "2026-10-22").sent_at == T1


def test_failure_is_recorded_and_not_marked_sent(session: Session, mailer: RecordingMailer) -> None:
    mailer.error = MailError("SMTP-сервер недоступен")

    result = deliver_once(session, mailer, KIND, "2026-10-22", Builder(), lambda: T1)

    assert result.outcome == Outcome.FAILED and "недоступен" in result.detail
    row = get_delivery(session, KIND, "2026-10-22")
    assert row.status == DeliveryStatus.FAILED
    assert row.failed_attempts == 1 and row.last_error == "SMTP-сервер недоступен"
    assert row.sent_at is None


def test_failed_delivery_is_retried_on_next_run_and_then_not_repeated(
    session: Session, mailer: RecordingMailer
) -> None:
    mailer.error = MailError("сбой")
    deliver_once(session, mailer, KIND, "k", Builder(), lambda: T1)
    deliver_once(session, mailer, KIND, "k", Builder(), lambda: T1)
    assert get_delivery(session, KIND, "k").failed_attempts == 2

    mailer.error = None
    retried = deliver_once(session, mailer, KIND, "k", Builder(), lambda: T2)
    again = deliver_once(session, mailer, KIND, "k", Builder(), lambda: T2)

    assert retried.outcome == Outcome.SENT and again.outcome == Outcome.SKIPPED
    row = get_delivery(session, KIND, "k")
    assert row.status == DeliveryStatus.SENT and row.last_error is None
    assert row.failed_attempts == 2  # история неудач сохраняется
    assert len(mailer.sent) == 1 and delivery_count(session) == 1


def test_force_sends_again_even_if_already_sent(session: Session, mailer: RecordingMailer) -> None:
    deliver_once(session, mailer, KIND, "k", Builder(), lambda: T1)

    result = deliver_force(session, mailer, KIND, "k", Builder(), lambda: T2)

    assert result.outcome == Outcome.SENT
    assert len(mailer.sent) == 2 and delivery_count(session) == 1
    assert get_delivery(session, KIND, "k").sent_at == T2


def test_failed_force_resend_does_not_undo_the_earlier_successful_send(
    session: Session, mailer: RecordingMailer
) -> None:
    deliver_once(session, mailer, KIND, "k", Builder(), lambda: T1)
    mailer.error = MailError("сбой")

    result = deliver_force(session, mailer, KIND, "k", Builder(), lambda: T2)

    assert result.outcome == Outcome.FAILED
    row = get_delivery(session, KIND, "k")
    assert row.status == DeliveryStatus.SENT and row.sent_at == T1


def test_same_key_for_different_kinds_is_independent(
    session: Session, mailer: RecordingMailer
) -> None:
    deliver_once(session, mailer, DeliveryKind.RATES_REPORT, "x", Builder(), lambda: T1)
    result = deliver_once(session, mailer, DeliveryKind.IEA_CHART, "x", Builder(), lambda: T1)

    assert result.outcome == Outcome.SENT
    assert delivery_count(session) == 2


def test_unreadable_attachment_is_a_recorded_failure_not_a_crash(
    session: Session, mailer: RecordingMailer
) -> None:
    def build() -> OutgoingMail:
        raise FileNotFoundError("chart.png")

    result = deliver_once(session, mailer, DeliveryKind.IEA_CHART, "c", build, lambda: T1)

    assert result.outcome == Outcome.FAILED
    assert get_delivery(session, DeliveryKind.IEA_CHART, "c").status == DeliveryStatus.FAILED
    assert mailer.sent == []


def test_built_mail_reaches_mailer_unchanged(session: Session, mailer: RecordingMailer) -> None:
    deliver_once(session, mailer, KIND, "k", Builder(), lambda: T1)

    assert mailer.sent[0] == OutgoingMail(subject="Тема", body="Тело")
