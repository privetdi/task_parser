"""Отправка писем: реальный SMTP либо режим без отправки (письмо сохраняется как .eml)."""
import re
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Literal, Protocol

from app.config import Settings

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class MailError(Exception):
    """Письмо не отправлено. Текст рассчитан на человека и не содержит секретов."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str  # например "image/png"


@dataclass(frozen=True)
class OutgoingMail:
    subject: str
    body: str
    attachments: tuple[Attachment, ...] = ()


class Mailer(Protocol):
    recipients: list[str]

    def send(self, mail: OutgoingMail) -> str:
        """Отправляет письмо всем получателям; возвращает пояснение для журнала.

        Бросает MailError, если письмо не ушло.
        """
        ...


def build_message(mail: OutgoingMail, sender: str, recipients: list[str]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = mail.subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or "localhost")
    message.set_content(mail.body)
    for attachment in mail.attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message


class FileMailer:
    """Режим без реальной отправки: полностью собранное письмо кладётся в каталог как .eml."""

    def __init__(self, directory: Path, sender: str, recipients: list[str]) -> None:
        self._directory = directory
        self._sender = sender
        self.recipients = recipients

    def send(self, mail: OutgoingMail) -> str:
        message = build_message(mail, self._sender, self.recipients)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path = self._directory / self._file_name(mail.subject)
            path.write_bytes(message.as_bytes())
        except OSError as exc:
            raise MailError(f"Не удалось сохранить письмо в {self._directory}: {exc}") from exc
        return f"сохранено без отправки: {path}"

    @staticmethod
    def _file_name(subject: str) -> str:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        slug = re.sub(r"[^\w-]+", "_", subject).strip("_")[:60]
        # uuid: два письма в одну секунду с одинаковой темой не затрут друг друга
        return f"{stamp}_{slug}_{uuid.uuid4().hex[:6]}.eml"


class SmtpMailer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        security: Literal["starttls", "ssl", "none"],
        user: str,
        password: str,
        sender: str,
        recipients: list[str],
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._security = security
        self._user = user
        self._password = password
        self._sender = sender
        self.recipients = recipients
        self._timeout = timeout

    def send(self, mail: OutgoingMail) -> str:
        message = build_message(mail, self._sender, self.recipients)
        server = f"{self._host}:{self._port}"
        smtp_class = smtplib.SMTP_SSL if self._security == "ssl" else smtplib.SMTP
        try:
            with smtp_class(self._host, self._port, timeout=self._timeout) as smtp:
                if self._security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if self._user:
                    smtp.login(self._user, self._password)
                refused = smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(
                f"SMTP-сервер {server} не принял логин или пароль: {self._clean(exc)}"
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(
                f"SMTP-сервер {server} отклонил всех получателей: {', '.join(exc.recipients)}"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(
                f"Не удалось отправить письмо через {server}: "
                f"{type(exc).__name__}: {self._clean(exc)}"
            ) from exc

        note = f"отправлено через {server}"
        if refused:
            # Часть получателей приняла письмо, поэтому повтор отправки создал бы дубли
            note += f"; сервер отклонил: {', '.join(refused)}"
        return note

    def _clean(self, exc: Exception) -> str:
        """Текст ошибки без пароля — на случай, если сервер или библиотека его повторят."""
        text = str(exc)
        return text.replace(self._password, "***") if self._password else text


def build_mailer(settings: Settings) -> Mailer:
    if settings.mail_mode == "smtp":
        return SmtpMailer(
            host=settings.smtp_host,
            port=settings.smtp_port,
            security=settings.smtp_security,
            user=settings.smtp_user,
            password=settings.smtp_password.get_secret_value(),
            sender=settings.mail_from,
            recipients=settings.mail_to,
        )
    return FileMailer(settings.outbox_dir, settings.mail_from, settings.mail_to)
