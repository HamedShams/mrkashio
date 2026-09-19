"""Bring older messages in.

Telegram bots never see messages sent before they joined, so history arrives either as text copied out of
a Telegram client (the "Name, [date]:" dump format) or as a Telegram Desktop JSON export. Both are parsed
here into plain messages and queued in the inbox; the normal sync then processes them.

An import never inserts what is already there. A message already in the inbox (same id, or the same send
time and text as a message the bot recorded live) is skipped. A message whose day and wording match a row
that is already on the transactions tab is held as a possible duplicate, whatever its amount says, and
waits for a person: /review lists such messages, /review keep queues one anyway, /review done closes it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from config import Settings
from sheets import STATUS_DUPLICATE, SheetEntry, SheetStore, message_key
from sync import format_amount, rollover_date, split_note

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
MAX_LISTED = 8  # how many dismissed or held messages a report lists in full

# Words that say how much or in what, not what was bought; ignored when comparing a message with a sheet row.
AMOUNT_WORDS = frozenset((
    "tl", "try", "lira", "lir", "eur", "euro", "euros", "usd", "dollar", "dollars", "gbp", "pound", "pounds", "toman", "tuman",
    "k", "bin", "hezar", "m", "million", "milyon", "لیر", "لیره", "تومان", "تومن", "یورو", "دلار", "پوند", "هزار", "میلیون",
))
AMOUNT_TOKEN = re.compile(r"[\d.,]+(tl|try|k|m|eur|usd|gbp)?")
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
GROCERIES_PREFIX = "groceries - "  # the prefix the prompt adds to grocery stores


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
    duplicates: int  # already in the inbox: same id, or same send time and text
    start: date | None
    dismissed: list[str]  # human-readable lines for the messages dated before the requested start
    notes: int = 0  # messages that were only a private note (NOTE_KEYWORD)
    held: list[str] = field(default_factory=list)  # messages that look already present in the sheet; waiting in /review

    def describe(self) -> str:
        lines = [f"Found {self.found} message(s); queued {self.imported} for the next sync."]
        if self.held:
            lines.append(f"⚠️ Held back {len(self.held)} that look already in the sheet (same day, same wording; the amount is not compared). "
                         "Nothing was queued for them. /review lists them; /review keep <n> queues one anyway, /review done <n> closes it:")
            lines.extend(f"  • {item}" for item in self.held[:MAX_LISTED])
            if len(self.held) > MAX_LISTED:
                lines.append(f"  • … and {len(self.held) - MAX_LISTED} more (see /review)")
        if self.before_start:
            lines.append(f"Dismissed {self.before_start} dated before {self.start:%d/%m/%Y}, as requested:")
            lines.extend(f"  • {item}" for item in self.dismissed[:MAX_LISTED])
            if self.before_start > MAX_LISTED:
                lines.append(f"  • … and {self.before_start - MAX_LISTED} more")
        if self.duplicates:
            lines.append(f"Skipped {self.duplicates} already in the inbox (same send time and text, whatever their status).")
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
    """Queue messages as pending; skip what the inbox has, hold what the sheet seems to have, dismiss anything before `since`."""
    known_ids = store.stored_message_ids()
    known_keys = store.stored_message_keys()
    on_sheet = by_day(store.transaction_index())
    rows: list[tuple[int, str, datetime, datetime | None, str, str]] = []
    held: list[tuple[int, str, datetime, datetime | None, str, str]] = []
    dismissed: list[str] = []
    held_lines: list[str] = []
    duplicates = notes = 0
    for message in messages:
        kept, had_note = split_note(message.text, settings.note_keyword)
        if had_note and not kept.strip():
            notes += 1
            continue
        message.text = kept
        day = rollover_date(message.sent_at, settings.day_rollover_hour)
        if since and day < since:
            dismissed.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: {_excerpt(message.text)}")
            continue
        key = message_key(message.sent_at.strftime("%Y-%m-%d %H:%M"), message.text)
        if message.message_id in known_ids or key in known_keys:
            duplicates += 1
            continue
        known_ids.add(message.message_id)
        known_keys.add(key)
        candidates = on_sheet.get(day, []) + (on_sheet.get(message.sent_at.date(), []) if message.sent_at.date() != day else [])
        match = find_duplicate(message.text, candidates)
        if match is not None:
            where = f"row {match.row}: {match.date:%d/%m/%Y} · {match.description} · {_amount(match)}"
            held.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text,
                         f"looks already in the sheet ({where}); the amount was not compared. /review keep queues it anyway"))
            held_lines.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: “{_excerpt(message.text)}” ≈ {where}")
            continue
        rows.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text, ""))
    if rows:
        store.add_messages(rows)
    if held:
        store.add_messages(held, status=STATUS_DUPLICATE)
    return ImportResult(len(messages), len(rows), len(dismissed), duplicates, since, dismissed, notes, held_lines)


def by_day(entries: list[SheetEntry]) -> dict[date, list[SheetEntry]]:
    index: dict[date, list[SheetEntry]] = {}
    for entry in entries:
        index.setdefault(entry.date, []).append(entry)
    return index


def find_duplicate(text: str, entries: list[SheetEntry]) -> SheetEntry | None:
    """The first sheet row of the same day whose description matches the message's wording, or None."""
    for entry in entries:
        if same_wording(text, entry.description):
            return entry
    return None


def same_wording(text: str, description: str) -> bool:
    """True when the words that describe the purchase are the same, ignoring amounts, currencies, digits, case and symbols.

    "Migros 450 tl" matches "Groceries - Migros"; "Barbershop 💈 (arash) 604 TL" matches "Barbershop"; a message that
    lists several items matches a row for any one of them. Amounts are deliberately not compared.
    """
    if description.casefold().startswith(GROCERIES_PREFIX):
        description = description[len(GROCERIES_PREFIX):]
    ours, theirs = set(_describing_words(text)), set(_describing_words(description))
    if not ours or not theirs:
        return False
    return theirs <= ours or ours <= theirs


def _describing_words(text: str) -> list[str]:
    cleaned = re.sub(r"[^\w\s]", " ", text.translate(DIGITS).casefold())
    return [word for word in cleaned.split() if word not in AMOUNT_WORDS and not AMOUNT_TOKEN.fullmatch(word)]


def _amount(entry: SheetEntry) -> str:
    try:
        return format_amount(float(entry.amount), entry.currency)
    except (TypeError, ValueError):
        return f"{entry.amount} {entry.currency}".strip()


def _excerpt(text: str) -> str:
    return " | ".join(part.strip() for part in text.splitlines() if part.strip())[:60]


def _dump_message(sender: str, sent_at: datetime, edited_at: datetime | None, text: str) -> DumpMessage:
    digest = hashlib.md5(f"{sender}|{sent_at.isoformat()}|{text}".encode("utf-8")).hexdigest()
    return DumpMessage(-int(digest[:12], 16), _first_name(sender), sent_at, edited_at, text)


def _first_name(full_name: str) -> str:
    parts = full_name.split()
    return parts[0] if parts else "unknown"


def _parse_time(value: str | None, settings: Settings) -> datetime | None:
    if not value:
        return None
    cleaned = " ".join(value.replace(" ", " ").replace(" ", " ").split())  # iOS uses narrow spaces before AM/PM
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
