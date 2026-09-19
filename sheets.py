"""Everything that touches the Google Sheet: the inbox, the run log, the pairing config and the transactions tab.

The transactions tab layout is configurable (COLUMN_* variables); by default B date, C amount, D currency,
E description, G category. Columns not listed are never touched.

Rows the bot writes carry a small note on the date cell, "kashio:<message id>", so that an edited Telegram
message can later find and update or remove exactly its own rows and nothing else. Notes travel with the
row when rows are sorted, moved or deleted by hand.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypeVar

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from config import ConfigError, Settings

log = logging.getLogger(__name__)

TIMESTAMP = "%Y-%m-%d %H:%M:%S"
NOTE_PREFIX = "kashio:"

INBOX_HEADERS = ("message_id", "sender", "sent_at", "edited_at", "text", "status", "processed_at", "rows_added", "note")
RUNS_HEADERS = (
    "run_at", "trigger", "requested_by", "status", "pending", "processed", "rows_added", "sheet_range",
    "skipped", "needs_review", "input_tokens", "output_tokens", "cost_usd", "model", "effort", "error", "merged",
    "unanswered", "revised", "rows_updated", "rows_deleted", "calls",
)
CONFIG_HEADERS = ("key", "value", "updated_at", "updated_by")
TARGET_HEADER_NAMES = {"date": "Date", "amount": "Amount", "currency": "Currency", "description": "Description", "category": "Category"}

STATUS_PENDING = "pending"
STATUS_PENDING_REVISION = "pending_revision"  # edited after its rows were written; the rows get replaced at the next sync
STATUS_PENDING_DELETION = "pending_deletion"  # deleted in Telegram after its rows were written; the rows go at the next sync
STATUS_DELETED = "deleted"  # rows removed because the message was deleted in Telegram; the inbox row stays for tracing
STATUS_SUPERSEDED = "superseded"  # an earlier version of a message that was edited later; kept so the chain of edits can be read
PENDING_STATUSES = (STATUS_PENDING, STATUS_PENDING_REVISION, STATUS_PENDING_DELETION)
STATUS_PROCESSED = "processed"
STATUS_SKIPPED = "skipped"
STATUS_MERGED = "merged"  # folded into another message's transaction (an amount sent separately, a correction)
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_EDITED_AFTER_SYNC = "edited_after_sync"  # no longer produced; kept so old inbox rows still read

# Number format for the amount column, so the symbol shown matches the currency column.
CURRENCY_FORMATS = {
    "TRY": ("CURRENCY", "[$₺]#,##0.0"),
    "EUR": ("CURRENCY", "[$€]#,##0.0"),
    "USD": ("CURRENCY", "[$$]#,##0.0"),
    "GBP": ("CURRENCY", "[$£]#,##0.0"),
    "TOMAN": ("NUMBER", '#,##0 "TOMAN"'),
}

T = TypeVar("T")


def _retry(call: Callable[[], T], attempts: int = 3) -> T:
    """Run a Sheets API call, retrying transient failures with a short backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except APIError as exc:
            if attempt == attempts:
                raise
            log.warning("Sheets API error (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


@dataclass
class InboxMessage:
    """One raw Telegram message as stored in the inbox tab."""

    row: int  # 1-based row number in the inbox tab
    message_id: int
    sender: str
    sent_at: datetime  # aware, in the configured time zone
    edited_at: datetime | None
    text: str
    status: str
    rows_added: int = 0  # rows this message produced earlier (revisions only)

    @property
    def revision(self) -> bool:
        return self.status == STATUS_PENDING_REVISION

    @property
    def deletion(self) -> bool:
        return self.status == STATUS_PENDING_DELETION


@dataclass
class TransactionRow:
    """One row to write to the transactions tab."""

    date: date
    amount: float
    currency: str
    description: str
    category: str
    message_id: int


@dataclass
class Replacement:
    """What replace_transactions did for one message."""

    updated: int = 0
    deleted: int = 0
    appended: int = 0
    rows: list[int] | None = None  # row numbers now holding the message's transactions


class SheetStore:
    """Thin wrapper over the tabs Kashio uses. Creates the hidden tabs, and the transactions tab, if missing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.columns = settings.columns
        if settings.google_service_account is None:
            raise ConfigError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Create a Google service account, download its JSON key, share the "
                "spreadsheet with the account's e-mail as Editor, and set GOOGLE_SERVICE_ACCOUNT_JSON (the key as one line) "
                "or GOOGLE_SERVICE_ACCOUNT_FILE (a path to it)."
            )
        client = gspread.service_account_from_dict(settings.google_service_account)
        self.service_account_email = settings.google_service_account.get("client_email", "the service account")
        self.spreadsheet = self._open_spreadsheet(client)
        self.target = self._ensure_target(settings.sheet_tab)
        self.inbox = self._ensure_tab(settings.inbox_tab, INBOX_HEADERS)
        self.runs = self._ensure_tab(settings.runs_tab, RUNS_HEADERS)
        self.config = self._ensure_tab(settings.config_tab, CONFIG_HEADERS)

    # ---------------------------------------------------------------- opening

    def _open_spreadsheet(self, client: gspread.Client) -> gspread.Spreadsheet:
        """Open GOOGLE_SHEET_ID, or find the spreadsheet shared with the service account when the id is not set."""
        wanted = self.settings.google_sheet_id
        if wanted:
            try:
                return client.open_by_key(wanted)
            except (SpreadsheetNotFound, APIError) as exc:
                raise ConfigError(
                    f"GOOGLE_SHEET_ID {wanted!r} could not be opened ({exc.__class__.__name__}). Check the id in the "
                    f"spreadsheet URL and share the spreadsheet with {self.service_account_email} as Editor."
                ) from exc
        try:
            files = client.list_spreadsheet_files()
        except APIError as exc:
            raise ConfigError(
                "GOOGLE_SHEET_ID is not set and the spreadsheet could not be listed. Either set GOOGLE_SHEET_ID, or enable "
                f"the Google Drive API in the Cloud project of {self.service_account_email} so it can be found automatically. "
                f"({exc})"
            ) from exc
        if not files:
            raise ConfigError(f"No spreadsheet is shared with {self.service_account_email}. Share yours with it as Editor, or set GOOGLE_SHEET_ID.")
        candidates = [client.open_by_key(f["id"]) for f in files]
        if len(candidates) > 1:
            with_tab = [sp for sp in candidates if any(ws.title == self.settings.sheet_tab for ws in sp.worksheets())]
            if len(with_tab) == 1:
                candidates = with_tab
        if len(candidates) != 1:
            names = "; ".join(f"{sp.title} ({sp.id})" for sp in candidates)
            raise ConfigError(f"{len(candidates)} spreadsheets are shared with {self.service_account_email} and none or several have a "
                              f"tab called {self.settings.sheet_tab!r}: {names}. Set GOOGLE_SHEET_ID to the one you want.")
        log.info("Using spreadsheet %r (%s), found through the Drive API", candidates[0].title, candidates[0].id)
        return candidates[0]

    def _ensure_target(self, title: str) -> gspread.Worksheet:
        """The transactions tab. Created with a header row when missing, so a fresh spreadsheet works too."""
        try:
            return self.spreadsheet.worksheet(title)
        except WorksheetNotFound:
            existing = ", ".join(ws.title for ws in self.spreadsheet.worksheets())
            log.warning("Tab %r not found (existing tabs: %s); creating it with a header row", title, existing)
            width = max(self.columns.index(letter) for letter in self.columns.written) + 1
            sheet = self.spreadsheet.add_worksheet(title=title, rows=1000, cols=max(width, 8))
            headers = [""] * width
            for field_name, name in TARGET_HEADER_NAMES.items():
                headers[self.columns.index(getattr(self.columns, field_name))] = name
            _retry(lambda: sheet.update([headers], "A1"))
            self._format_new_target(sheet)
            return sheet

    def _format_new_target(self, sheet: gspread.Worksheet) -> None:
        """Date and number formats for a tab the bot created; cosmetic, never blocks."""
        c = self.columns
        try:
            _retry(lambda: sheet.format(f"{c.date}2:{c.date}", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}))
            _retry(lambda: sheet.format(f"{c.amount}2:{c.amount}", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}))
            _retry(lambda: sheet.format("1:1", {"textFormat": {"bold": True}}))
        except APIError as exc:
            log.warning("Could not format the new tab: %s", exc)

    def _ensure_tab(self, title: str, headers: Sequence[str]) -> gspread.Worksheet:
        """Return the tab, creating it if needed. A tab made by hand gets its header row and is hidden too."""
        try:
            sheet = self.spreadsheet.worksheet(title)
        except WorksheetNotFound:
            log.info("Creating tab %r", title)
            sheet = self.spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        current = _retry(lambda: sheet.row_values(1))
        if current[: len(headers)] != list(headers):  # new tab, hand-made tab, or headers extended in a newer version
            _retry(lambda: sheet.update([list(headers)], "A1"))
        if not sheet.isSheetHidden:
            try:
                sheet.hide()
            except APIError:
                log.warning("Could not hide tab %r; it stays visible", title)
        return sheet

    # ------------------------------------------------------------------ inbox

    def add_message(self, message_id: int, sender: str, sent_at: datetime, text: str, note: str = "") -> None:
        row = [str(message_id), sender, sent_at.strftime(TIMESTAMP), "", text, STATUS_PENDING, "", "", note]
        _retry(lambda: self.inbox.append_row(row, value_input_option="RAW"))

    def add_skipped(self, message_id: int, sender: str, sent_at: datetime, label: str, reason: str) -> None:
        """Log that a message was seen and skipped without storing its content: media, or a private note."""
        now = datetime.now(self.settings.timezone).strftime(TIMESTAMP)
        row = [str(message_id), sender, sent_at.strftime(TIMESTAMP), "", f"[{label}]", STATUS_SKIPPED, now, 0, reason]
        _retry(lambda: self.inbox.append_row(row, value_input_option="RAW"))

    def add_messages(self, rows: Sequence[tuple[int, str, datetime, datetime | None, str]]) -> None:
        """Bulk insert of (message_id, sender, sent_at, edited_at, text), used by the backfill command."""
        values = [
            [str(mid), sender, sent.strftime(TIMESTAMP), edited.strftime(TIMESTAMP) if edited else "", text, STATUS_PENDING, "", "", ""]
            for mid, sender, sent, edited, text in rows
        ]
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            _retry(lambda: self.inbox.append_rows(chunk, value_input_option="RAW"))

    def stored_message_ids(self) -> set[int]:
        ids: set[int] = set()
        for value in _retry(lambda: self.inbox.col_values(1))[1:]:
            try:
                ids.add(int(value))
            except ValueError:
                continue
        return ids

    def latest_row(self, message_id: int) -> tuple[int, list[str]] | None:
        """The newest inbox row for a message (edits append rows), as (row number, values), or None."""
        found: tuple[int, list[str]] | None = None
        for index, values in enumerate(_retry(self.inbox.get_all_values)[1:], start=2):
            if values and values[0] == str(message_id):
                found = (index, list(values) + [""] * (len(INBOX_HEADERS) - len(values)))
        return found

    def update_message(self, message_id: int, text: str, edited_at: datetime, retire_reason: str | None = None) -> str | None:
        """Record an edit as a new inbox row and return the new row's status, or None if the message was never stored.

        The inbox is an audit trail: the previous row is kept and marked `superseded`, the new row carries the
        edited text. Its status says what the next sync should do:
        - the message was still pending: `pending` again (or `skipped`, with `retire_reason`, when edited into a note)
        - it was skipped or flagged without any row written: `pending`, so the next sync looks at it again
        - it already has rows in the sheet: `pending_revision`, and the sync replaces those rows with what the
          edited text now says (or removes them, when the message was edited into a note)
        """
        latest = self.latest_row(message_id)
        if latest is None:
            return None
        index, values = latest
        previous = values[5] or STATUS_PENDING
        rows_added = int(values[7]) if str(values[7]).isdigit() else 0
        has_rows = rows_added > 0 or previous in (STATUS_PENDING_REVISION, STATUS_PENDING_DELETION)
        now = datetime.now(self.settings.timezone).strftime(TIMESTAMP)
        if has_rows:
            status = STATUS_PENDING_REVISION
            note = "retracted after an edit; its rows will be removed" if retire_reason else "edited after its rows were written; they will be replaced"
            processed_at, carried = "", rows_added
        elif retire_reason:
            status, note, processed_at, carried = STATUS_SKIPPED, retire_reason, now, 0
        else:
            status, processed_at, carried = STATUS_PENDING, "", ""
            note = "edited" if previous == STATUS_PENDING else "reopened after an edit"
        new_row = [str(message_id), values[1], values[2], edited_at.strftime(TIMESTAMP), text, status, processed_at, carried, note]
        _retry(lambda: self.inbox.batch_update([
            {"range": f"F{index}", "values": [[STATUS_SUPERSEDED]]},
            {"range": f"I{index}", "values": [[f"superseded by an edit at {edited_at.strftime(TIMESTAMP)}; was {previous}"]]},
        ]))
        _retry(lambda: self.inbox.append_row(new_row, value_input_option="RAW"))
        return status

    def pending_messages(self) -> list[InboxMessage]:
        rows = _retry(self.inbox.get_all_values)
        seen: set[int] = set()
        pending: list[InboxMessage] = []
        for index, values in enumerate(rows[1:], start=2):
            values = list(values) + [""] * (len(INBOX_HEADERS) - len(values))
            message_id, sender, sent_at, edited_at, text, status, _processed, rows_added = values[:8]
            try:
                message_id = int(message_id)  # negative ids mark messages imported from a chat export
            except ValueError:
                continue
            if status not in PENDING_STATUSES or message_id in seen:
                continue
            try:
                parsed_sent = self._parse_timestamp(sent_at)
                parsed_edited = self._parse_timestamp(edited_at) if edited_at else None
            except ValueError:
                log.warning("Inbox row %s has an unreadable timestamp (%r); skipping it", index, sent_at)
                continue
            seen.add(message_id)
            pending.append(InboxMessage(
                row=index, message_id=message_id, sender=sender, sent_at=parsed_sent, edited_at=parsed_edited,
                text=text, status=status, rows_added=int(rows_added) if str(rows_added).isdigit() else 0,
            ))
        return pending

    def messages_with_rows(self) -> list[tuple[int, int]]:
        """(inbox row, message id) of live Telegram messages whose rows are in the sheet: the ones a deletion could orphan."""
        found: list[tuple[int, int]] = []
        for index, values in enumerate(_retry(self.inbox.get_all_values)[1:], start=2):
            values = list(values) + [""] * (len(INBOX_HEADERS) - len(values))
            try:
                message_id = int(values[0])
            except ValueError:
                continue
            rows_added = int(values[7]) if str(values[7]).isdigit() else 0
            if message_id > 0 and rows_added > 0 and values[5] in (STATUS_PROCESSED, STATUS_NEEDS_REVIEW, STATUS_PENDING_REVISION):
                found.append((index, message_id))
        return found

    def mark_deleted(self, rows: Sequence[int]) -> None:
        """Queue messages deleted in Telegram for row removal. Text and history stay in the inbox for tracing."""
        if not rows:
            return
        when = datetime.now(self.settings.timezone).strftime(TIMESTAMP)
        updates = [{"range": f"F{row}", "values": [[STATUS_PENDING_DELETION]]} for row in rows]
        updates += [{"range": f"I{row}", "values": [[f"deleted in Telegram, noticed {when}; its rows will be removed"]]} for row in rows]
        _retry(lambda: self.inbox.batch_update(updates))

    def mark_messages(self, marks: Sequence[tuple[int, str, int, str]]) -> None:
        """Set status, processed_at, rows_added and note for the given inbox rows: (row, status, rows_added, note)."""
        if not marks:
            return
        now = datetime.now(self.settings.timezone).strftime(TIMESTAMP)
        updates = [
            {"range": f"F{row}:I{row}", "values": [[status, now, rows_added, note]]}
            for row, status, rows_added, note in marks
        ]
        _retry(lambda: self.inbox.batch_update(updates))

    def _parse_timestamp(self, value: str) -> datetime:
        return datetime.strptime(value, TIMESTAMP).replace(tzinfo=self.settings.timezone)

    # ----------------------------------------------------------------- config

    def read_config(self) -> dict[str, str]:
        """Key/value pairs stored by /setup (group id, admin id). Environment variables take precedence."""
        return {row[0]: row[1] for row in _retry(self.config.get_all_values)[1:] if len(row) >= 2 and row[0]}

    def write_config(self, values: dict[str, object], updated_by: str) -> None:
        now = datetime.now(self.settings.timezone).strftime(TIMESTAMP)
        rows = _retry(self.config.get_all_values)
        position = {row[0]: index for index, row in enumerate(rows[1:], start=2) if row and row[0]}
        updates, appends = [], []
        for key, value in values.items():
            if key in position:
                updates.append({"range": f"B{position[key]}:D{position[key]}", "values": [[str(value), now, updated_by]]})
            else:
                appends.append([key, str(value), now, updated_by])
        if updates:
            _retry(lambda: self.config.batch_update(updates))
        if appends:
            _retry(lambda: self.config.append_rows(appends, value_input_option="RAW"))

    # ---------------------------------------------------------------- run log

    def log_run(self, values: Sequence[object]) -> None:
        _retry(lambda: self.runs.append_row(list(values), value_input_option="RAW"))

    # ----------------------------------------------------------- transactions

    def category_options(self) -> list[str]:
        """Allowed values of the category column's dropdown on the target tab, or [] when there is no dropdown.

        Handles both a fixed list and a list fed from a range (Google's budget template points the column at a
        category table in its Summary tab). Reading it on every sync keeps the sheet the source of truth.
        """
        probe = f"'{self.settings.sheet_tab}'!{self.columns.category}{self.last_used_row()}"
        meta = _retry(lambda: self.spreadsheet.fetch_sheet_metadata({"includeGridData": True, "ranges": [probe]}))
        try:
            cell = meta["sheets"][0]["data"][0]["rowData"][0]["values"][0]
        except (KeyError, IndexError):
            return []
        condition = cell.get("dataValidation", {}).get("condition", {})
        values = [entry.get("userEnteredValue", "") for entry in condition.get("values", [])]
        if condition.get("type") == "ONE_OF_LIST":
            return values
        if condition.get("type") == "ONE_OF_RANGE" and values:
            source = values[0].lstrip("=").replace("$", "")
            rows = _retry(lambda: self.spreadsheet.values_get(source)).get("values", [])
            return [value for row in rows for value in row if value]
        return []

    def last_recorded_date(self) -> date | None:
        """The latest date in the date column of the target tab, or None when the tab holds no dates yet."""
        latest: date | None = None
        column = self.columns.date
        for row in _retry(lambda: self.target.get_values(f"{column}1:{column}", value_render_option="UNFORMATTED_VALUE")):
            for value in row:
                parsed = _as_date(value)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
        return latest

    def last_used_row(self) -> int:
        """Highest row with a value in the date, amount or description column. Pre-filled currency cells do not count."""
        c = self.columns
        first, last = min(c.written, key=c.index), max(c.written, key=c.index)
        keep = {c.index(letter) - c.index(first) for letter in (c.date, c.amount, c.description)}
        values = _retry(lambda: self.target.get_values(f"{first}1:{last}"))
        used = 1
        for index, row in enumerate(values, start=1):
            row = list(row) + [""] * (c.index(last) - c.index(first) + 1)
            if any(row[offset] for offset in keep):
                used = index
        return used

    def append_transactions(self, rows: Sequence[TransactionRow]) -> tuple[int, int]:
        """Write rows under the last used row and return the (first, last) row numbers written."""
        if not rows:
            raise ValueError("no rows to append")
        last = self.last_used_row()
        start, end = last + 1, last + len(rows)
        if end > self.target.row_count:
            _retry(lambda: self.target.add_rows(end - self.target.row_count))
        self._assert_empty(start, end)
        self._copy_format(source_row=last, start=start, end=end)
        self._write_rows(start, rows)
        return start, end

    def rows_for_message(self, message_id: int) -> list[int]:
        """Row numbers on the target tab whose date cell carries this message's note."""
        column = self.columns.date
        notes = _retry(lambda: self.target.get_notes(grid_range=f"{column}1:{column}"))
        wanted = f"{NOTE_PREFIX}{message_id}"
        return [index for index, row in enumerate(notes, start=1) if row and row[0] == wanted]

    def replace_transactions(self, message_id: int, rows: Sequence[TransactionRow]) -> Replacement:
        """Make the sheet reflect an edited message: update its rows in place, delete extra ones, append missing ones.

        Only rows carrying this message's own note are touched. Rows are re-checked right before deletion.
        """
        old = self.rows_for_message(message_id)
        result = Replacement()
        keep = min(len(old), len(rows))
        if keep:
            for row_number, row in zip(old[:keep], rows[:keep]):
                self._write_rows(row_number, [row])
            result.updated = keep
        for row_number in sorted(old[keep:], reverse=True):  # bottom-up, so earlier row numbers stay valid
            current = _retry(lambda: self.target.get_notes(grid_range=f"{self.columns.date}{row_number}"))
            if not current or not current[0] or current[0][0] != f"{NOTE_PREFIX}{message_id}":
                raise RuntimeError(f"refusing to delete row {row_number}: it no longer carries the note of message {message_id}")
            _retry(lambda: self.target.delete_rows(row_number))
            result.deleted += 1
        if len(rows) > keep:
            start, _end = self.append_transactions(rows[keep:])
            result.appended = len(rows) - keep
            result.rows = old[:keep] + list(range(start, start + result.appended))
        else:
            result.rows = old[:keep]
        return result

    def _write_rows(self, start: int, rows: Sequence[TransactionRow]) -> None:
        """Values for the configured columns, the currency formats, and the provenance notes."""
        c = self.columns
        end = start + len(rows) - 1
        updates = [
            {"range": f"{c.date}{start}:{c.date}{end}", "values": [[r.date.isoformat()] for r in rows]},
            {"range": f"{c.amount}{start}:{c.amount}{end}", "values": [[r.amount] for r in rows]},
            {"range": f"{c.currency}{start}:{c.currency}{end}", "values": [[r.currency] for r in rows]},
            {"range": f"{c.description}{start}:{c.description}{end}", "values": [[r.description] for r in rows]},
            {"range": f"{c.category}{start}:{c.category}{end}", "values": [[r.category] for r in rows]},
        ]
        _retry(lambda: self.target.batch_update(updates, value_input_option="USER_ENTERED"))
        self._apply_currency_formats(start, [r.currency for r in rows])
        try:
            _retry(lambda: self.target.insert_notes({f"{c.date}{start + i}": f"{NOTE_PREFIX}{r.message_id}" for i, r in enumerate(rows)}))
        except APIError as exc:  # provenance is needed for later edits, but must not lose the data write
            log.warning("Could not write provenance notes for rows %s-%s: %s", start, end, exc)

    def _assert_empty(self, start: int, end: int) -> None:
        """Guardrail: the bot only ever appends. Refuse to write if the destination rows hold anything but a default currency."""
        c = self.columns
        first, last = min(c.written, key=c.index), max(c.written, key=c.index)
        width = c.index(last) - c.index(first) + 1
        check = [c.index(letter) - c.index(first) for letter in c.written if letter != c.currency]
        existing = _retry(lambda: self.target.get_values(f"{first}{start}:{last}{end}"))
        for offset, row in enumerate(existing):
            row = list(row) + [""] * width
            if any(row[i] for i in check):
                raise RuntimeError(
                    f"refusing to write: row {start + offset} of {self.settings.sheet_tab!r} already holds data; nothing was written"
                )

    def _apply_currency_formats(self, start: int, currencies: Sequence[str]) -> None:
        """Make the symbol in the amount column match the currency column (the copied format would show the previous row's)."""
        requests = currency_format_requests(self.target.id, start, currencies, self.columns.index(self.columns.amount))
        if not requests:
            return
        try:
            _retry(lambda: self.spreadsheet.batch_update({"requests": requests}))
        except APIError as exc:  # cosmetic; never block the data write on it
            log.warning("Could not set currency formats: %s", exc)

    def _copy_format(self, source_row: int, start: int, end: int) -> None:
        """Repeat the formatting of `source_row` (borders, number and date formats, dropdowns) over the new rows."""
        c = self.columns
        first, last = min(c.written, key=c.index), max(c.written, key=c.index)
        sheet_id = self.target.id
        request = {
            "copyPaste": {
                "source": {"sheetId": sheet_id, "startRowIndex": source_row - 1, "endRowIndex": source_row,
                           "startColumnIndex": c.index(first), "endColumnIndex": c.index(last) + 1},
                "destination": {"sheetId": sheet_id, "startRowIndex": start - 1, "endRowIndex": end,
                                "startColumnIndex": c.index(first), "endColumnIndex": c.index(last) + 1},
                "pasteType": "PASTE_FORMAT",
                "pasteOrientation": "NORMAL",
            }
        }
        try:
            _retry(lambda: self.spreadsheet.batch_update({"requests": [request]}))
        except APIError as exc:  # formatting is cosmetic; never block the data write on it
            log.warning("Could not copy row formatting: %s", exc)


def currency_format_requests(sheet_id: int, start: int, currencies: Sequence[str], column_index: int = 2) -> list[dict]:
    """One repeatCell request per run of equal currencies, for the amount column from row `start` (1-based)."""
    requests: list[dict] = []
    index = 0
    while index < len(currencies):
        last = index
        while last + 1 < len(currencies) and currencies[last + 1] == currencies[index]:
            last += 1
        fmt = CURRENCY_FORMATS.get(currencies[index])
        if fmt:
            kind, pattern = fmt
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": start - 1 + index, "endRowIndex": start + last,
                          "startColumnIndex": column_index, "endColumnIndex": column_index + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat",
            }})
        index = last + 1
    return requests


def _as_date(value: object) -> date | None:
    """A Google Sheets cell as a date: serial numbers (days since 1899-12-30) or common text formats."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 20000 < value < 80000:  # 1954 .. 2119
        return date(1899, 12, 30) + timedelta(days=int(value))
    if isinstance(value, str):
        for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                continue
    return None
