import smtplib
from typing import Any, ClassVar

import pytest

from app.services.mailer import Attachment, MailError, OutgoingMail, SmtpMailer

PASSWORD = "s3cret-пароль"


class FakeSMTP:
    """Подмена smtplib.SMTP: запоминает, что с ней делали, и умеет ломаться по заказу."""

    instances: ClassVar[list["FakeSMTP"]] = []
    connect_error: ClassVar[Exception | None] = None
    login_error: ClassVar[Exception | None] = None
    send_result: ClassVar[Any] = {}  # dict отказов или исключение

    use_ssl = False

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        if FakeSMTP.connect_error:
            raise FakeSMTP.connect_error
        self.host, self.port, self.timeout = host, port, timeout
        self.tls_started = False
        self.logged_in_as: tuple[str, str] | None = None
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.tls_started = True

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        self.logged_in_as = (user, password)

    def send_message(self, message: Any) -> dict:
        if isinstance(FakeSMTP.send_result, Exception):
            raise FakeSMTP.send_result
        self.sent.append(message)
        return FakeSMTP.send_result


class FakeSMTPSsl(FakeSMTP):
    use_ssl = True


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances = []
    FakeSMTP.connect_error = FakeSMTP.login_error = None
    FakeSMTP.send_result = {}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSsl)


def make_mailer(security: str = "starttls", user: str = "robot", **kwargs: Any) -> SmtpMailer:
    return SmtpMailer(
        host="smtp.example.com", port=587, security=security, user=user, password=PASSWORD,
        sender="rates@example.com", recipients=["a@example.com", "b@example.com"], **kwargs,
    )


MAIL = OutgoingMail(
    subject="Курсы",
    body="Отчёт",
    attachments=(Attachment("r.xlsx", b"data", "application/x-test"),),
)


def test_starttls_sends_through_login_and_reports_server() -> None:
    note = make_mailer("starttls").send(MAIL)

    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.tls_started and not smtp.use_ssl
    assert smtp.logged_in_as == ("robot", PASSWORD)
    message = smtp.sent[0]
    assert message["To"] == "a@example.com, b@example.com"
    assert message["Subject"] == "Курсы"
    assert next(message.iter_attachments()).get_filename() == "r.xlsx"
    assert "smtp.example.com:587" in note


def test_ssl_mode_uses_ssl_connection_without_starttls() -> None:
    make_mailer("ssl").send(MAIL)

    smtp = FakeSMTP.instances[0]
    assert smtp.use_ssl and not smtp.tls_started


def test_none_mode_uses_plain_connection() -> None:
    make_mailer("none").send(MAIL)

    smtp = FakeSMTP.instances[0]
    assert not smtp.use_ssl and not smtp.tls_started


def test_login_is_skipped_when_no_user_configured() -> None:
    make_mailer("none", user="").send(MAIL)

    assert FakeSMTP.instances[0].logged_in_as is None


def test_authentication_error_is_explained_and_does_not_leak_password() -> None:
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(
        535, f"Bad credentials for {PASSWORD}".encode()
    )

    with pytest.raises(MailError) as exc:
        make_mailer().send(MAIL)

    assert "логин или пароль" in str(exc.value)
    assert PASSWORD not in str(exc.value)


def test_unreachable_server_error_names_the_server() -> None:
    FakeSMTP.connect_error = ConnectionRefusedError("Connection refused")

    with pytest.raises(MailError) as exc:
        make_mailer().send(MAIL)

    assert "smtp.example.com:587" in str(exc.value)
    assert "ConnectionRefusedError" in str(exc.value)


def test_all_recipients_refused_is_an_error() -> None:
    FakeSMTP.send_result = smtplib.SMTPRecipientsRefused(
        {"a@example.com": (550, b"no"), "b@example.com": (550, b"no")}
    )

    with pytest.raises(MailError, match="отклонил всех получателей: a@example.com, b@example.com"):
        make_mailer().send(MAIL)


def test_partial_refusal_counts_as_sent_but_is_mentioned() -> None:
    FakeSMTP.send_result = {"b@example.com": (550, b"no such user")}

    note = make_mailer().send(MAIL)

    assert "b@example.com" in note


def test_generic_smtp_error_is_wrapped() -> None:
    FakeSMTP.send_result = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    with pytest.raises(MailError, match="SMTPServerDisconnected"):
        make_mailer().send(MAIL)
