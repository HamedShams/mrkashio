"""Everything that touches the Google Sheet: the inbox, the run log and the transactions tab.

Layout of the transactions tab (fixed by the spreadsheet, not by this code):
    B date · C amount · D currency · E description · F untouched · G category
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

INBOX_HEADERS = ("message_id", "sender", "sent_at", "edited_at", "text", "status", "processed_at", "rows_added", "note")
RUNS_HEADERS = (
    "run_at", "trigger", "requested_by", "status", "pending", "processed", "rows_added", "sheet_range",
    "skipped", "needs_review", "input_tokens", "output_tokens", "cost_usd", "model", "effort", "error", "merged",
)

STATUS_PENDING = "pending"
STATUS_PROCESSED = "processed"
STATUS_SKIPPED = "skipped"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_MERGED = "merged"  # folded into another message's transaction (an amount sent separately, a correction)
STATUS_EDITED_AFTER_SYNC = "edited_after_sync"
CONFIG_HEADERS = ("key", "value", "updated_at", "updated_by")
TARGET_HEADERS = ("", "Date", "Amount", "Currency", "Description", "By", "Category")  # A is left free

# Zero-based column bounds of the block we format on the transactions tab: B (1) through G (7, exclusive).
FIRST_COLUMN_INDEX, END_COLUMN_INDEX = 1, 7

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


@dataclass
class TransactionRow:
    """One row to append to the transactions tab."""

    date: date
    amount: float
    currency: str
    description: str
    category: str
    message_id: int


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


class SheetStore:
    """Thin wrapper over the three tabs Kashio uses. Creates the hidden tabs if they are missing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        client = gspread.service_account_from_dict(settings.google_service_account)
        self.service_account_email = settings.google_service_account.get("client_email", "the service account")
        self.spreadsheet = self._open_spreadsheet(client)
        self.target = self._ensure_target(settings.sheet_tab)
        self.inbox = self._ensure_tab(settings.inbox_tab, INBOX_HEADERS)
        self.runs = self._ensure_tab(settings.runs_tab, RUNS_HEADERS)
        self.config = self._ensure_tab(settings.config_tab, CONFIG_HEADERS)

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
            sheet = self.spreadsheet.add_worksheet(title=title, rows=1000, cols=len(TARGET_HEADERS))
            _retry(lambda: sheet.update([list(TARGET_HEADERS)], "A1"))
            self._format_new_target(sheet)
            return sheet

    def _format_new_target(self, sheet: gspread.Worksheet) -> None:
        """Date and number formats for a tab the bot created; cosmetic, never blocks."""
        try:
            _retry(lambda: sheet.format("B2:B", {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}))
            _retry(lambda: sheet.format("C2:C", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}))
            _retry(lambda: sheet.format("A1:G1", {"textFormat": {"bold": True}}))
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

    def add_message(self, message_id: int, sender: str, sent_at: datetime, text: str) -> None:
        row = [str(message_id), sender, sent_at.strftime(TIMESTAMP), "", text, STATUS_PENDING, "", "", ""]
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

    def update_message(self, message_id: int, text: str, edited_at: datetime) -> str | None:
        """Store the edited text. Returns the status the message had before the edit, or None if it was never stored."""
        cell = _retry(lambda: self.inbox.find(str(message_id), in_column=1))
        if cell is None:
            return None
        previous = (_retry(lambda: self.inbox.row_values(cell.row)) + [""] * 9)[5] or STATUS_PENDING
        updates = [{"range": f"D{cell.row}:E{cell.row}", "values": [[edited_at.strftime(TIMESTAMP), text]]}]
        if previous != STATUS_PENDING:
            updates.append({
                "range": f"F{cell.row}",
                "values": [[STATUS_EDITED_AFTER_SYNC]],
            })
            updates.append({
                "range": f"I{cell.row}",
                "values": [[f"edited after sync (was {previous}); sheet not changed"]],
            })
        _retry(lambda: self.inbox.batch_update(updates))
        return previous

    def pending_messages(self) -> list[InboxMessage]:
        rows = _retry(self.inbox.get_all_values)
        seen: set[int] = set()
        pending: list[InboxMessage] = []
        for index, values in enumerate(rows[1:], start=2):
            values = list(values) + [""] * (len(INBOX_HEADERS) - len(values))
            message_id, sender, sent_at, edited_at, text, status = values[:6]
            try:
                message_id = int(message_id)  # negative ids mark messages imported from a chat export
            except ValueError:
                continue
            if status != STATUS_PENDING or message_id in seen:  # a retried append could store a message twice
                continue
            seen.add(message_id)
            pending.append(
                InboxMessage(
                    row=index,
                    message_id=message_id,
                    sender=sender,
                    sent_at=self._parse_timestamp(sent_at),
                    edited_at=self._parse_timestamp(edited_at) if edited_at else None,
                    text=text,
                    status=status,
                )
            )
        return pending

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
        """Allowed values of the column-G dropdown on the target tab, or [] when there is no dropdown.

        Handles both a fixed list and a list fed from a range (Google's budget template points G at a
        category table in its Summary tab). Reading it on every sync keeps the sheet the source of truth.
        """
        probe = f"'{self.settings.sheet_tab}'!G{self.last_used_row()}"
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
        """The latest date in column B of the target tab, or None when the tab holds no dates yet."""
        latest: date | None = None
        for row in _retry(lambda: self.target.get_values("B1:B", value_render_option="UNFORMATTED_VALUE")):
            for value in row:
                parsed = _as_date(value)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
        return latest

    def last_used_row(self) -> int:
        """Highest row with a value in B, C or E. Pre-filled currency cells in D do not count."""
        values = _retry(lambda: self.target.get_values("B1:E"))
        last = 1
        for index, row in enumerate(values, start=1):
            b, c, _, e = (list(row) + [""] * 4)[:4]
            if b or c or e:
                last = index
        return last

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
        updates = [
            {
                "range": f"B{start}:E{end}",
                "values": [[r.date.isoformat(), r.amount, r.currency, r.description] for r in rows],
            },
            {"range": f"G{start}:G{end}", "values": [[r.category] for r in rows]},
        ]
        _retry(lambda: self.target.batch_update(updates, value_input_option="USER_ENTERED"))
        return start, end

    def _assert_empty(self, start: int, end: int) -> None:
        """Guardrail: the bot only ever appends. Refuse to write if the destination rows hold anything but a default currency."""
        existing = _retry(lambda: self.target.get_values(f"B{start}:G{end}"))
        for offset, row in enumerate(existing):
            b, c, _d, e, f, g = (list(row) + [""] * 6)[:6]
            if any((b, c, e, f, g)):
                raise RuntimeError(
                    f"refusing to write: row {start + offset} of {self.settings.sheet_tab!r} already holds data; nothing was written"
                )

    def _copy_format(self, source_row: int, start: int, end: int) -> None:
        """Repeat the formatting of `source_row` (borders, number and date formats, dropdowns) over the new rows."""
        sheet_id = self.target.id
        request = {
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": source_row - 1,
                    "endRowIndex": source_row,
                    "startColumnIndex": FIRST_COLUMN_INDEX,
                    "endColumnIndex": END_COLUMN_INDEX,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": start - 1,
                    "endRowIndex": end,
                    "startColumnIndex": FIRST_COLUMN_INDEX,
                    "endColumnIndex": END_COLUMN_INDEX,
                },
                "pasteType": "PASTE_FORMAT",
                "pasteOrientation": "NORMAL",
            }
        }
        try:
            _retry(lambda: self.spreadsheet.batch_update({"requests": [request]}))
        except APIError as exc:  # formatting is cosmetic; never block the data write on it
            log.warning("Could not copy row formatting: %s", exc)
