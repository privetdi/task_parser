"""Общие помощники тестов."""
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Читает JSON-фикстуру (фрагмент реального ответа API)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordingMailer:
    """Почтальон для тестов: запоминает письма и по заказу «ломается» (поле `error`)."""

    def __init__(self) -> None:
        self.recipients = ["a@example.com", "b@example.com"]
        self.sent: list = []
        self.error: Exception | None = None

    def send(self, mail) -> str:
        if self.error:
            raise self.error
        self.sent.append(mail)
        return "отправлено через test"
