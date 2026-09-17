"""Bring older messages in.

Telegram bots never see messages sent before they joined, so history arrives either as text copied out of
a Telegram client (the "Name, [date]:" dump format) or as a Telegram Desktop JSON export. Both are parsed
here into plain messages and queued in the inbox; the normal sync then processes them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from config import Settings
from sheets import SheetStore
from sync import rollover_date, split_note

# "Hamed Shams, [24 Jul 2026 at 21:46:10 (24 Jul 2026 at 23:45:01)]:"  (the part in parentheses is the edit time)
# Tolerant on purpose: the comma, the spaces and the trailing colon are all optional.
HEADER = re.compile(r"^(?P<sender>[^\[\]\n]+?)\s*,?\s*\[(?P<sent>[^\[\]()]+?)(?:\s*\((?P<edited>[^()]+?)\))?\]\s*:?\s*$")
TIME_FORMATS = (
    "%d %b %Y at %H:%M:%S", "%d %b %Y at %H:%M",  # Telegram for macOS / iOS copy, 24-hour
    "%d %b %Y at %I:%M:%S %p", "%d %b %Y at %I:%M %p",  # same, 12-hour clock
    "%d %B %Y at %H:%M:%S", "%d %B %Y at %H:%M", "%b %d, %Y at %I:%M:%S %p", "%b %d, %Y at %I:%M %p",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",  # Telegram Desktop copy
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y, %H:%M",
)
MAX_LISTED = 8  # how many dismissed messages a report lists in full


@dataclass
class DumpMessage:
    message_id: int  # negative, so it can never collide with a live Telegram message id
    sender: str
    sent_at: datetime
    edited_at: datetime | None
    text: str


@dataclass
class Parsed:
    messages: list[DumpMessage]
    unparsed: list[str]  # non-empty lines that appeared before any recognisable header

    def problems(self) -> str | None:
        if not self.unparsed:
            return None
        shown = "; ".join(line[:60] for line in self.unparsed[:3])
        return f"{len(self.unparsed)} line(s) came before the first message header and were ignored: {shown}"


@dataclass
class ImportResult:
    found: int
    imported: int
    before_start: int
    duplicates: int
    start: date | None
    dismissed: list[str]  # human-readable lines for the messages skipped as already covered by the sheet
    notes: int = 0  # messages that were only a private note (NOTE_KEYWORD)

    def describe(self) -> str:
        lines = [f"Found {self.found} message(s); queued {self.imported} for the next sync."]
        if self.before_start:
            lines.append(f"Dismissed {self.before_start} dated before {self.start:%d/%m/%Y}, the day after the sheet's last entry:")
            lines.extend(f"  • {item}" for item in self.dismissed[:MAX_LISTED])
            if self.before_start > MAX_LISTED:
                lines.append(f"  • … and {self.before_start - MAX_LISTED} more")
        if self.duplicates:
            lines.append(f"Skipped {self.duplicates} already queued or processed earlier (same sender, time and text).")
        if self.notes:
            lines.append(f"Skipped {self.notes} private note(s); notes are never stored or sent to Claude.")
        return "\n".join(lines)


def parse(content: str, settings: Settings) -> Parsed:
    """Auto-detect a Telegram Desktop JSON export or a pasted dump."""
    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            return Parsed(parse_export(json.loads(stripped), settings), [])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    return parse_dump(content, settings)


def parse_dump(content: str, settings: Settings) -> Parsed:
    """Text copied out of a Telegram chat: a header line per message, then the message body."""
    messages: list[DumpMessage] = []
    unparsed: list[str] = []
    header: tuple[str, datetime, datetime | None] | None = None
    body: list[str] = []

    def flush() -> None:
        if header is None:
            return
        text = "\n".join(body).strip()
        if text:
            sender, sent_at, edited_at = header
            messages.append(_dump_message(sender, sent_at, edited_at, text))

    for line in content.splitlines():
        match = HEADER.match(line.strip())
        if match:
            sent_at = _parse_time(match["sent"], settings)
            if sent_at is not None:
                flush()
                edited_at = _parse_time(match["edited"], settings) if match["edited"] else None
                header, body = (match["sender"].strip(), sent_at, edited_at), []
                continue
        if header is not None:
            body.append(line)
        elif line.strip():
            unparsed.append(line.strip())
    flush()
    return Parsed(messages, unparsed)


def parse_export(export: dict, settings: Settings) -> list[DumpMessage]:
    """Telegram Desktop → Export chat history → JSON (result.json)."""
    messages: list[DumpMessage] = []
    for entry in export.get("messages", []):
        if entry.get("type") != "message":
            continue
        text = _export_text(entry.get("text"))
        if not text.strip():
            continue
        sent_at = _export_time(entry.get("date_unixtime"), entry.get("date"), settings)
        edited_at = _export_time(entry.get("edited_unixtime"), entry.get("edited"), settings) if entry.get("edited") else None
        sender = _first_name(entry.get("from") or "unknown")
        messages.append(DumpMessage(-int(entry["id"]), sender, sent_at, edited_at, text))
    return messages


def import_messages(
    store: SheetStore, settings: Settings, messages: list[DumpMessage], since: date | None = None
) -> ImportResult:
    """Queue messages as pending, skipping days the sheet already covers and anything already stored."""
    last = store.last_recorded_date()
    start = since or (last + timedelta(days=1) if last else None)
    known = store.stored_message_ids()
    rows: list[tuple[int, str, datetime, datetime | None, str]] = []
    dismissed: list[str] = []
    duplicates = notes = 0
    for message in messages:
        kept, had_note = split_note(message.text, settings.note_keyword)
        if had_note and not kept.strip():
            notes += 1
            continue
        message.text = kept
        if start and rollover_date(message.sent_at, settings.day_rollover_hour) < start:
            excerpt = " | ".join(part.strip() for part in message.text.splitlines() if part.strip())[:60]
            dismissed.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: {excerpt}")
            continue
        if message.message_id in known:
            duplicates += 1
            continue
        known.add(message.message_id)
        rows.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text))
    if rows:
        store.add_messages(rows)
    return ImportResult(len(messages), len(rows), len(dismissed), duplicates, start, dismissed, notes)


def _dump_message(sender: str, sent_at: datetime, edited_at: datetime | None, text: str) -> DumpMessage:
    digest = hashlib.md5(f"{sender}|{sent_at.isoformat()}|{text}".encode("utf-8")).hexdigest()
    return DumpMessage(-int(digest[:12], 16), _first_name(sender), sent_at, edited_at, text)


def _first_name(full_name: str) -> str:
    parts = full_name.split()
    return parts[0] if parts else "unknown"


def _parse_time(value: str | None, settings: Settings) -> datetime | None:
    if not value:
        return None
    cleaned = " ".join(value.replace("\u202f", " ").replace("\u00a0", " ").split())  # iOS uses narrow spaces before AM/PM
    for pattern in TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, pattern).replace(tzinfo=settings.timezone)
        except ValueError:
            continue
    return None


def _export_text(value: object) -> str:
    """Telegram exports text either as a string or as a list of strings and formatted segments."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part if isinstance(part, str) else str(part.get("text", "")) for part in value)
    return ""


def _export_time(unix: object, iso: object, settings: Settings) -> datetime:
    if unix:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc).astimezone(settings.timezone)
    return datetime.fromisoformat(str(iso)).replace(tzinfo=settings.timezone)
