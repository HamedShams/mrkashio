"""Settings for Kashio. Every value comes from an environment variable.

Railway injects variables directly. For local runs, a `.env` file next to this
file is loaded automatically (see `.env.example`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
REQUIRED = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY")


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


def _text(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _integer(name: str, default: int | None) -> int | None:
    raw = _text(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _number(name: str, default: float) -> float:
    raw = _text(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _flag(name: str, default: bool) -> bool:
    raw = _text(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _service_account() -> dict:
    """The Google key: either the JSON itself (GOOGLE_SERVICE_ACCOUNT_JSON) or a path to the downloaded file."""
    raw = _text("GOOGLE_SERVICE_ACCOUNT_JSON")
    path = _text("GOOGLE_SERVICE_ACCOUNT_FILE")
    if raw is None and path is None:
        raise ConfigError("set GOOGLE_SERVICE_ACCOUNT_JSON (the key file as one line) or GOOGLE_SERVICE_ACCOUNT_FILE (path to it)")
    try:
        if raw is None:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(raw)
    except OSError as exc:
        raise ConfigError(f"GOOGLE_SERVICE_ACCOUNT_FILE {path!r} cannot be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. In a .env file, wrap the whole value in single quotes "
            "or use GOOGLE_SERVICE_ACCOUNT_FILE=path/to/key.json instead"
        ) from exc


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_bot_token: str
    telegram_chat_id: int | None  # the group to record; optional, /setup in the group stores it in the sheet
    telegram_admin_chat_id: int | None  # where run reports go; optional, /setup stores it too; falls back to the group
    # Anthropic
    anthropic_api_key: str
    anthropic_model: str
    anthropic_effort: str
    price_input_per_million: float  # USD, used only to estimate cost in the run log
    price_output_per_million: float
    # Google Sheets
    google_service_account: dict
    google_sheet_id: str | None  # optional: found through the Drive API when empty
    sheet_tab: str
    inbox_tab: str
    runs_tab: str
    config_tab: str
    # Behaviour
    sync_cron: str
    timezone: ZoneInfo
    day_rollover_hour: int
    default_currency: str
    scheduled_min_messages: int
    manual_min_messages: int
    post_summary: bool

    @classmethod
    def from_env(cls) -> Settings:
        missing = [name for name in REQUIRED if not _text(name)]
        if missing:
            raise ConfigError("missing required variables: " + ", ".join(missing))

        service_account = _service_account()
        if "client_email" not in service_account or "private_key" not in service_account:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON does not look like a service-account key file")

        tz_name = _text("TIMEZONE", "Europe/Istanbul")
        try:
            timezone = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"TIMEZONE {tz_name!r} is not a known time zone") from exc

        effort = _text("ANTHROPIC_EFFORT", "high").lower()
        if effort not in EFFORT_LEVELS:
            raise ConfigError(f"ANTHROPIC_EFFORT must be one of {', '.join(EFFORT_LEVELS)}, got {effort!r}")

        rollover = _integer("DAY_ROLLOVER_HOUR", 4)
        if not 0 <= rollover <= 23:
            raise ConfigError("DAY_ROLLOVER_HOUR must be between 0 and 23")

        return cls(
            telegram_bot_token=_text("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_integer("TELEGRAM_CHAT_ID", None),
            telegram_admin_chat_id=_integer("TELEGRAM_ADMIN_CHAT_ID", None),
            anthropic_api_key=_text("ANTHROPIC_API_KEY"),
            anthropic_model=_text("ANTHROPIC_MODEL", "claude-sonnet-5"),
            anthropic_effort=effort,
            price_input_per_million=_number("ANTHROPIC_PRICE_INPUT_PER_MILLION", 2.0),
            price_output_per_million=_number("ANTHROPIC_PRICE_OUTPUT_PER_MILLION", 10.0),
            google_service_account=service_account,
            google_sheet_id=_text("GOOGLE_SHEET_ID"),  # None → discovered at startup
            sheet_tab=_text("SHEET_TAB", "Transactions_Trip#2"),
            inbox_tab=_text("INBOX_TAB", "Bot_Inbox"),
            runs_tab=_text("RUNS_TAB", "Bot_Runs"),
            config_tab=_text("CONFIG_TAB", "Bot_Config"),
            sync_cron=_text("SYNC_CRON", "0 9 1,15 * *"),
            timezone=timezone,
            day_rollover_hour=rollover,
            default_currency=_text("DEFAULT_CURRENCY", "TRY").upper(),
            scheduled_min_messages=max(1, _integer("SCHEDULED_MIN_MESSAGES", 5)),
            manual_min_messages=max(1, _integer("MANUAL_MIN_MESSAGES", 1)),
            post_summary=_flag("POST_SUMMARY", True),
        )
