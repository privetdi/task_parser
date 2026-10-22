from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def make(**env: str) -> Settings:
    """Настройки только из переданных значений: `.env` не читаем."""
    return Settings(_env_file=None, **env)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_defaults_work_without_any_env() -> None:
    s = make()
    assert s.mail_mode == "dry-run"
    assert s.timezone == "Europe/Minsk"
    assert s.backfill_start == date(2016, 7, 1)
    assert s.cron_rates_report == "0 9 * * *"
    assert s.database_url == f"sqlite:///{Path('../data') / 'rates.db'}"


def test_recipients_are_parsed_from_comma_separated_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_TO", "a@example.com, b@example.com ,")
    assert make().mail_to == ["a@example.com", "b@example.com"]


def test_smtp_mode_without_host_explains_what_is_missing() -> None:
    with pytest.raises(ValidationError) as exc:
        make(mail_mode="smtp")
    assert "SMTP_HOST" in str(exc.value)


def test_smtp_mode_with_host_is_valid() -> None:
    s = make(mail_mode="smtp", smtp_host="localhost")
    assert s.smtp_host == "localhost"


def test_empty_recipients_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_TO", " , ")
    with pytest.raises(ValidationError) as exc:
        make()
    assert "MAIL_TO" in str(exc.value)


def test_invalid_cron_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        make(cron_rates_sync="каждый день")
    assert "cron" in str(exc.value)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(timezone="Mars/Olympus")


def test_password_is_not_shown_in_repr() -> None:
    s = make(smtp_password="s3cret")
    assert "s3cret" not in repr(s)
    assert s.smtp_password.get_secret_value() == "s3cret"


def test_iea_browser_defaults_keep_profile_inside_data_dir() -> None:
    s = make(data_dir="/srv/rates")

    assert s.iea_browser_channel == "chromium"
    assert s.iea_headless is True
    assert s.iea_profile == Path("/srv/rates/browser-profile")


def test_iea_profile_dir_can_be_set_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IEA_PROFILE_DIR", "/profiles/iea")

    assert make().iea_profile == Path("/profiles/iea")


def test_iea_browser_channel_accepts_chrome_and_rejects_unknown_values() -> None:
    assert make(iea_browser_channel="chrome").iea_browser_channel == "chrome"
    with pytest.raises(ValidationError):
        make(iea_browser_channel="firefox")
