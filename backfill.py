"""Bring older messages in.

Telegram bots never see messages sent before they joined, so history arrives either as text copied out of
a Telegram client (the "Name, [date]:" dump format) or as a Telegram Desktop JSON export. Both are parsed
here into plain messages and queued in the inbox; the normal sync then processes them.

An import never inserts what is already there. A message already in the inbox (same id, or the same send
time and text as a message the bot recorded live) is skipped. A message with the same sender and send time
as a stored one but different text is that message, corrected: it is queued as a revision, and the sync
updates the rows it produced earlier, exactly as an edit in Telegram would. A message that repeats a row
already on the transactions tab (same day, same wording, same amount and currency) is held as a duplicate
and waits for a person: /review lists such messages, /review keep queues one anyway, /review done closes it.

Lines that start with "..." are reported: Telegram sometimes leaves a line (Persian text, typically) out of
a multi-message copy and shows "..." instead, so the amount under it has lost its description.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from config import Settings
from sheets import STATUS_DUPLICATE, STATUS_PENDING, STATUS_PENDING_REVISION, InboxMessage, SheetEntry, SheetStore, normalised_text
from sync import format_amount, rollover_date, split_note

SAME_MESSAGE_SECONDS = 120  # Telegram's copy rounds send times (17:04:59 is shown as 17:05:00); within this, same text means same message

CURRENCY_WORDS = {
    "TRY": ("tl", "try", "lira", "lir", "₺", "لیر", "لیره"),
    "EUR": ("eur", "euro", "euros", "€", "یورو"),
    "USD": ("usd", "dollar", "dollars", "$", "دلار"),
    "GBP": ("gbp", "pound", "pounds", "£", "پوند"),
    "TOMAN": ("toman", "tuman", "تومان", "تومن"),
}

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
NUMBER = re.compile(r"(?<![\w.,])(\d[\d.,]*)\s*(k|bin|hezar|m|milyon|million)?(?![\w.,])", re.IGNORECASE)
MULTIPLIER = {"k": 1000, "bin": 1000, "hezar": 1000, "m": 1_000_000, "milyon": 1_000_000, "million": 1_000_000}
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
    ignored: int = 0  # the bot's own messages (reports, replies), left out of the import

    def problems(self) -> str | None:
        if not self.unparsed:
            return None
        shown = "; ".join(line[:60] for line in self.unparsed[:3])
        return f"{len(self.unparsed)} lines came before the first message header and were ignored: {shown}"

    def notes(self) -> list[str]:
        lines = [self.problems()] if self.unparsed else []
        if self.ignored:
            lines.append(f"Left out {self.ignored} messages written by the bot itself.")
        return lines


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
    revised: list[str] = field(default_factory=list)  # stored messages whose text changed in this paste; their rows get updated
    cut: list[str] = field(default_factory=list)  # messages with a "..." line: something was probably left out of the copy

    def describe(self) -> str:
        lines = [f"Found {self.found} messages; queued {self.imported} for the next sync."]
        if self.revised:
            lines.append(f"✏️ {len(self.revised)} messages were already stored with a different text; the new text counts as a correction "
                         "and their rows will be updated at the sync:")
            lines.extend(f"  • {item}" for item in self.revised[:MAX_LISTED])
            if len(self.revised) > MAX_LISTED:
                lines.append(f"  • … and {len(self.revised) - MAX_LISTED} more")
        if self.cut:
            lines.append(f"⚠️ {len(self.cut)} messages contain a line that starts with \"...\". Telegram sometimes leaves a line out when "
                         "messages are copied (Persian text, typically), so the amount below it may have lost its description. "
                         "Check these in the group and paste them again if so:")
            lines.extend(f"  • {item}" for item in self.cut[:MAX_LISTED])
            if len(self.cut) > MAX_LISTED:
                lines.append(f"  • … and {len(self.cut) - MAX_LISTED} more")
        if self.held:
            lines.append(f"⚠️ Held back {len(self.held)} that look already recorded (a sheet row of the same day with the same wording, amount "
                         "and currency, or the same text recorded live on that day). Nothing was queued for them. /review lists them; "
                         "/review keep <n> queues one anyway, /review done <n> closes it:")
            lines.extend(f"  • {item}" for item in self.held[:MAX_LISTED])
            if len(self.held) > MAX_LISTED:
                lines.append(f"  • … and {len(self.held) - MAX_LISTED} more (see /review)")
        if self.before_start:
            lines.append(f"Dismissed {self.before_start} dated before {self.start:%d/%m/%Y}, as requested:")
            lines.extend(f"  • {item}" for item in self.dismissed[:MAX_LISTED])
            if self.before_start > MAX_LISTED:
                lines.append(f"  • … and {self.before_start - MAX_LISTED} more")
        if self.duplicates:
            lines.append(f"Skipped {self.duplicates} already in the inbox (same text within two minutes of the same send time, whatever their status).")
        if self.notes:
            lines.append(f"Skipped {self.notes} private notes; notes are never stored or sent to Claude.")
        return "\n".join(lines)


def parse(content: str, settings: Settings, ignore_sender: str | None = None) -> Parsed:
    """Auto-detect a Telegram Desktop JSON export or a pasted dump. `ignore_sender` is the bot's own display name."""
    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            return parse_export(json.loads(stripped), settings, ignore_sender)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    return parse_dump(content, settings, ignore_sender)


def parse_dump(content: str, settings: Settings, ignore_sender: str | None = None) -> Parsed:
    """Text copied out of a Telegram chat: a header line per message, then the message body."""
    messages: list[DumpMessage] = []
    unparsed: list[str] = []
    header: tuple[str, datetime, datetime | None] | None = None
    body: list[str] = []
    ignored = 0

    def flush() -> None:
        nonlocal ignored
        if header is None:
            return
        text = "\n".join(body).strip()
        if text:
            sender, sent_at, edited_at = header
            if _same_name(sender, ignore_sender):
                ignored += 1
            else:
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
    return Parsed(messages, unparsed, ignored)


def parse_export(export: dict, settings: Settings, ignore_sender: str | None = None) -> Parsed:
    """Telegram Desktop → Export chat history → JSON (result.json)."""
    messages: list[DumpMessage] = []
    ignored = 0
    for entry in export.get("messages", []):
        if entry.get("type") != "message":
            continue
        text = _export_text(entry.get("text"))
        if not text.strip():
            continue
        if _same_name(str(entry.get("from") or ""), ignore_sender):
            ignored += 1
            continue
        sent_at = _export_time(entry.get("date_unixtime"), entry.get("date"), settings)
        edited_at = _export_time(entry.get("edited_unixtime"), entry.get("edited"), settings) if entry.get("edited") else None
        sender = _first_name(entry.get("from") or "unknown")
        messages.append(DumpMessage(-int(entry["id"]), sender, sent_at, edited_at, text))
    return Parsed(messages, [], ignored)


def _same_name(sender: str, ignore_sender: str | None) -> bool:
    return bool(ignore_sender) and " ".join(sender.split()).casefold() == " ".join(ignore_sender.split()).casefold()


def import_messages(
    store: SheetStore, settings: Settings, messages: list[DumpMessage], since: date | None = None
) -> ImportResult:
    """Queue messages as pending; skip what the inbox has, hold what the sheet seems to have, dismiss anything before `since`."""
    known_ids = store.stored_message_ids()
    known_texts = store.stored_texts()
    stored = store.latest_rows()
    by_moment = sorted(stored.values(), key=lambda m: m.sent_at)
    on_sheet = by_day(store.transaction_index())
    rows: list[tuple[int, str, datetime, datetime | None, str, str]] = []
    held: list[tuple[int, str, datetime, datetime | None, str, str]] = []
    revisions: list[tuple[InboxMessage, str, object, str]] = []
    dismissed: list[str] = []
    held_lines: list[str] = []
    revised_lines: list[str] = []
    cut_lines: list[str] = []
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
        text_key = normalised_text(message.text)
        stored_times = known_texts.get(text_key, [])
        if message.message_id in known_ids or any(abs((message.sent_at - when).total_seconds()) <= SAME_MESSAGE_SECONDS for when in stored_times):
            duplicates += 1
            continue
        if any(line.strip().startswith(("...", "…")) for line in message.text.splitlines()):
            cut_lines.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: {_excerpt(message.text)}")
        same_moment = [m for m in by_moment if m.sender == message.sender and abs((m.sent_at - message.sent_at).total_seconds()) <= SAME_MESSAGE_SECONDS]
        if len(same_moment) == 1:  # the same message, stored with another text: this paste corrects it
            original = same_moment[0]
            revisions.append((original, message.text, original.rows_added if original.rows_added else "",
                              f"corrected by a paste on {datetime.now(message.sent_at.tzinfo):%Y-%m-%d}; its rows will be "
                              + ("updated" if original.rows_added else "written")))
            revised_lines.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: “{_excerpt(message.text)}” (was “{_excerpt(original.text)}”)")
            known_texts.setdefault(text_key, []).append(message.sent_at)
            continue
        if len(same_moment) > 1:  # Telegram copies several quick messages as one block: same block, or a changed one
            if normalised_text(" ".join(m.text for m in same_moment)) == text_key:
                duplicates += 1
                continue
            ids = ", ".join(str(m.message_id) for m in same_moment)
            held.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text,
                         f"spans {len(same_moment)} stored messages ({ids}) but its text differs from theirs; fix by hand or /review keep to import it whole"))
            held_lines.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: “{_excerpt(message.text)}” ≈ {len(same_moment)} stored messages, text differs")
            known_texts.setdefault(text_key, []).append(message.sent_at)
            continue
        known_ids.add(message.message_id)
        same_day = [when for when in stored_times if rollover_date(when, settings.day_rollover_hour) == day]
        known_texts.setdefault(text_key, []).append(message.sent_at)
        if same_day:  # the same words, the same day, another time: almost surely the same message, but a person decides
            when = same_day[0]
            held.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text,
                         f"the same text was recorded on this day at {when:%H:%M}; probably the same message. /review keep queues it anyway"))
            held_lines.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: “{_excerpt(message.text)}” ≈ recorded live at {when:%H:%M}")
            continue
        candidates = on_sheet.get(day, []) + (on_sheet.get(message.sent_at.date(), []) if message.sent_at.date() != day else [])
        match = find_duplicate(message.text, candidates, settings)
        if match is not None:
            where = f"row {match.row}: {match.date:%d/%m/%Y} · {match.description} · {_amount(match)}"
            held.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text,
                         f"already in the sheet ({where}): same day, wording, amount and currency. /review keep queues it anyway"))
            held_lines.append(f"{message.sent_at:%d/%m/%Y %H:%M} {message.sender}: “{_excerpt(message.text)}” ≈ {where}")
            continue
        rows.append((message.message_id, message.sender, message.sent_at, message.edited_at, message.text, ""))
    if rows:
        store.add_messages(rows)
    if held:
        store.add_messages(held, status=STATUS_DUPLICATE)
    if revisions:
        store.revise_messages(revisions)
    return ImportResult(len(messages), len(rows) + len(revisions), len(dismissed), duplicates, since, dismissed, notes,
                        held_lines, revised_lines, cut_lines)


def by_day(entries: list[SheetEntry]) -> dict[date, list[SheetEntry]]:
    index: dict[date, list[SheetEntry]] = {}
    for entry in entries:
        index.setdefault(entry.date, []).append(entry)
    return index


def find_duplicate(text: str, entries: list[SheetEntry], settings: Settings) -> SheetEntry | None:
    """The first sheet row of the same day that one of the message's items repeats, or None."""
    for item in text.split("\n\n"):  # a message may list several items, separated by blank lines
        for entry in entries:
            if same_item(item, entry, settings.default_currency):
                return entry
    return None


def same_item(item: str, entry: SheetEntry, default_currency: str) -> bool:
    """True when an item of a message and a sheet row say the same thing: same describing words, same amount, same currency.

    Words are compared without amounts, currency words, digits, symbols and case, and without the "Groceries - " prefix
    the prompt adds, so "Migros 450 tl" and "Migros 450" both repeat a row "Groceries - Migros · 450 TRY", while
    "Migros - Water 400 TL" does not repeat "Migros · 400 TRY" and "Migros 400" does not repeat "Migros · 450 TRY".
    """
    ours, theirs = _describing_words(item), _describing_words(entry.description)
    if not ours or not theirs or (set(ours) != set(theirs) and "".join(ours) != "".join(theirs)):  # "Carre four" is "Carrefour"
        return False
    if _currency_of(item, default_currency) != entry.currency.strip().upper():
        return False
    amount = _amount_value(entry.amount)
    return amount is not None and any(abs(candidate - amount) < 0.005 for candidate in _amounts_in(item))


def _describing_words(text: str) -> list[str]:
    lowered = text.translate(DIGITS).casefold().strip()
    if lowered.startswith(GROCERIES_PREFIX):
        lowered = lowered[len(GROCERIES_PREFIX):]
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    return [word for word in cleaned.split() if word not in AMOUNT_WORDS and not AMOUNT_TOKEN.fullmatch(word)]


def _currency_of(text: str, default_currency: str) -> str:
    lowered = text.casefold()
    words = set(re.sub(r"[^\w\s€$£₺]", " ", lowered).split())
    for code, markers in CURRENCY_WORDS.items():
        if any(marker in words or (not marker.isalpha() and marker in lowered) for marker in markers):
            return code
    return default_currency


def _amounts_in(text: str) -> set[float]:
    """Every number in the text, times k or m, read every way its separators can be meant.

    "1,158.4" is 1158.4 (the last separator is the decimal point when both appear); "266.50" is 266.5; "1.250" and
    "1,250" are 1250 or 1.25, so both readings count; "1.234.567" groups thousands.
    """
    found: set[float] = set()
    for digits, suffix in NUMBER.findall(text.translate(DIGITS)):
        factor = MULTIPLIER.get(suffix.lower(), 1) if suffix else 1
        separators = [ch for ch in digits if ch in ".,"]
        if not separators:
            readings = {digits}
        elif len(set(separators)) == 2:
            decimal = separators[-1]
            readings = {digits.replace("," if decimal == "." else ".", "").replace(decimal, ".")}
        elif len(separators) > 1:
            readings = {digits.replace(",", "").replace(".", "")}
        else:
            after = len(digits) - digits.index(separators[0]) - 1
            readings = {digits.replace(",", ".")} if after != 3 else {digits.replace(",", "."), digits.replace(",", "").replace(".", "")}
        for reading in readings:
            try:
                found.add(float(reading) * factor)
            except ValueError:
                continue
    return found


def _amount_value(value: object) -> float | None:
    """A sheet amount as a number; text such as "₺674" is read too, a note across the row is not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = NUMBER.search(str(value).translate(DIGITS))
    if not match or len(str(value)) > 24:
        return None
    return next(iter(sorted(_amounts_in(match.group(0)))), None)


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
