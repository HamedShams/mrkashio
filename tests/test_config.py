import json

import pytest

from config import ConfigError, Settings


def test_defaults_apply_when_optional_variables_are_absent(monkeypatch):
    for name in ("ANTHROPIC_MODEL", "SYNC_CRON", "SCHEDULED_MIN_MESSAGES", "MANUAL_MIN_MESSAGES", "POST_SUMMARY", "SHEET_TAB"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert s.anthropic_model == "claude-sonnet-5"
    assert s.sync_cron == "0 9 1,15 * *"
    assert (s.scheduled_min_messages, s.manual_min_messages) == (5, 1)
    assert s.post_summary is True
    assert s.sheet_tab == "Transactions_Trip#2"


def test_only_the_bot_token_is_required_to_start(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


def test_missing_integration_keys_do_not_stop_startup(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    s = Settings.from_env()  # the bot itself reports these as missing, with the fix
    assert s.anthropic_api_key is None and s.google_service_account is None


def test_invalid_effort_is_rejected_with_the_allowed_values(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_EFFORT", "turbo")
    with pytest.raises(ConfigError, match="low, medium, high, xhigh, max"):
        Settings.from_env()


def test_service_account_from_file(monkeypatch, tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"client_email": "file@test.iam.gserviceaccount.com", "private_key": "k"}))
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    assert Settings.from_env().google_service_account["client_email"] == "file@test.iam.gserviceaccount.com"


def test_multiline_json_error_points_to_the_fix(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{")
    with pytest.raises(ConfigError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        Settings.from_env()


def test_sheet_id_and_chat_ids_are_optional(monkeypatch):
    for name in ("GOOGLE_SHEET_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_CHAT_ID"):
        monkeypatch.delenv(name)
    s = Settings.from_env()
    assert s.google_sheet_id is None and s.telegram_chat_id is None and s.telegram_admin_chat_id is None
