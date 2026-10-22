from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from pathlib import Path

import pytest

from app.config import Settings
from app.services.mailer import (
    XLSX_MIME,
    Attachment,
    FileMailer,
    MailError,
    OutgoingMail,
    SmtpMailer,
    build_mailer,
)

RECIPIENTS = ["a@example.com", "b@example.com"]


def read_eml(path: Path) -> EmailMessage:
    return message_from_bytes(path.read_bytes(), policy=default)


def test_eml_is_written_and_readable_with_subject_recipients_and_attachment(tmp_path: Path) -> None:
    mailer = FileMailer(tmp_path / "outbox", "rates@example.com", RECIPIENTS)
    xlsx = b"PK\x03\x04 fake xlsx bytes"

    note = mailer.send(OutgoingMail(
        subject="Курсы НБ РБ на 2026-10-22",
        body="Отчёт во вложении",
        attachments=(Attachment("rates_2026-10-22.xlsx", xlsx, XLSX_MIME),),
    ))

    files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 1 and str(files[0]) in note
    message = read_eml(files[0])
    assert message["Subject"] == "Курсы НБ РБ на 2026-10-22"
    assert message["From"] == "rates@example.com"
    assert message["To"] == "a@example.com, b@example.com"
    assert message["Date"] and message["Message-ID"]
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "Отчёт во вложении"
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "rates_2026-10-22.xlsx"
    assert attachment.get_content_type() == XLSX_MIME
    assert attachment.get_content() == xlsx


def test_two_mails_with_same_subject_do_not_overwrite_each_other(tmp_path: Path) -> None:
    mailer = FileMailer(tmp_path, "rates@example.com", RECIPIENTS)
    for _ in range(2):
        mailer.send(OutgoingMail(subject="Тема", body="x"))

    assert len(list(tmp_path.glob("*.eml"))) == 2


def test_image_attachment_keeps_type(tmp_path: Path) -> None:
    mailer = FileMailer(tmp_path, "rates@example.com", RECIPIENTS)
    mailer.send(OutgoingMail("График", "x", (Attachment("chart.png", b"\x89PNG", "image/png"),)))

    attachment = next(read_eml(next(tmp_path.glob("*.eml"))).iter_attachments())
    assert attachment.get_content_type() == "image/png"


def test_unwritable_directory_is_a_mail_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("не каталог")
    mailer = FileMailer(blocker / "outbox", "rates@example.com", RECIPIENTS)

    with pytest.raises(MailError, match="Не удалось сохранить"):
        mailer.send(OutgoingMail("x", "y"))


def test_factory_picks_file_mailer_by_default_and_smtp_when_configured(tmp_path: Path) -> None:
    default_settings = Settings(_env_file=None, data_dir=tmp_path)
    assert isinstance(build_mailer(default_settings), FileMailer)

    smtp_settings = Settings(
        _env_file=None, mail_mode="smtp", smtp_host="smtp.example.com", data_dir=tmp_path
    )
    mailer = build_mailer(smtp_settings)
    assert isinstance(mailer, SmtpMailer)
    assert mailer.recipients == smtp_settings.mail_to
