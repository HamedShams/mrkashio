"""Shared fixtures. Everything runs offline: no Telegram, no Google, no Anthropic."""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

# Set before `config` is imported anywhere, so a local .env cannot leak into the tests.
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:test-token",
    "ANTHROPIC_API_KEY": "sk-test",
    "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account", "client_email": "bot@test.iam.gserviceaccount.com", "private_key": "k"}),
    "GOOGLE_SHEET_ID": "sheet-id",
    "TELEGRAM_CHAT_ID": "-1001",
    "TELEGRAM_ADMIN_CHAT_ID": "42",
    "TIMEZONE": "Europe/Istanbul",
    "DAY_ROLLOVER_HOUR": "4",
    "DEFAULT_CURRENCY": "TRY",
    "ANTHROPIC_EFFORT": "high",
})

from config import Settings  # noqa: E402

TZ = ZoneInfo("Europe/Istanbul")


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env()


def at(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)
