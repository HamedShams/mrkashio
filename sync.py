"""One sync run from start to finish: threshold check, Claude, row building, sheet writes, report.

The report is built in memory first and survives any failure, so the Telegram
notification always goes out even when the spreadsheet is unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import anthropic

from config import Settings
from extractor import Extraction, clean_categories, extract
from sheets import (
    STATUS_MERGED,
    STATUS_NEEDS_REVIEW,
    STATUS_PROCESSED,
    STATUS_SKIPPED,
    InboxMessage,
    SheetStore,
    TransactionRow,
)

log = logging.getLogger(__name__)

TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"  # /sync in Telegram
TRIGGER_CLI = "cli"  # python bot.py sync

STATUS_OK = "ok"
STATUS_DRY_RUN = "dry_run"
STATUS_SKIPPED_THRESHOLD = "skipped_threshold"
STATUS_FAILED = "failed"

CURRENCY_SYMBOLS = {"TRY": "₺", "EUR": "€", "USD": "$", "GBP": "£"}
MAX_REVIEW_ITEMS_IN_REPORT = 12


@dataclass
class ReviewItem:
    message_id: int
    sender: str
    sent_at: datetime
    text: str
    note: str


@dataclass
class RunReport:
    trigger: str
    requested_by: str
    started_at: datetime
    model: str
    effort: str
    status: str = ""
    threshold: int = 0
    pending: int = 0
    processed: int = 0
    rows: list[TransactionRow] = field(default_factory=list)
    sheet_range: str = ""
    skipped: int = 0
    merged: int = 0  # messages folded into another message's transaction
    review: list[ReviewItem] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    categories: list[str] = field(default_factory=list)  # the category names in force for this run
    last_recorded: date | None = None  # latest date already in the sheet before this run
    error: str | None = None
    log_error: str | None = None  # set when writing the run log itself failed

    def totals_by_currency(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.rows:
            totals[row.currency] = totals.get(row.currency, 0.0) + row.amount
        return totals

    def as_row(self) -> list[object]:
        """Values in the order of sheets.RUNS_HEADERS."""
        return [
            self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            self.trigger,
            self.requested_by,
            self.status,
            self.pending,
            self.processed,
            len(self.rows),
            self.sheet_range,
            self.skipped,
            len(self.review),
            self.input_tokens,
            self.output_tokens,
            round(self.cost_usd, 4),
            self.model,
            self.effort,
            self.error or "",
            self.merged,
        ]


def rollover_date(sent_at: datetime, rollover_hour: int) -> date:
    """A note written at 01:28 is about yesterday's spending: anything before the rollover hour counts for the previous day."""
    if sent_at.hour < rollover_hour:
        return (sent_at - timedelta(days=1)).date()
    return sent_at.date()


def parse_override_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def sanitize_description(text: str) -> str:
    """Collapse whitespace and stop a leading =, +, - or @ from being read as a formula by the spreadsheet."""
    text = " ".join(text.split())
    return f"'{text}" if text[:1] in "=+-@" else text


def run_sync(
    settings: Settings,
    store: SheetStore,
    client: anthropic.Anthropic,
    system_prompt: str,
    *,
    trigger: str,
    requested_by: str,
    dry_run: bool = False,
) -> RunReport:
    report = RunReport(
        trigger=trigger,
        requested_by=requested_by,
        started_at=datetime.now(settings.timezone),
        model=settings.anthropic_model,
        effort=settings.anthropic_effort,
    )
    report.threshold = (
        settings.scheduled_min_messages if trigger == TRIGGER_SCHEDULE else settings.manual_min_messages
    )
    try:
        pending = store.pending_messages()
        report.pending = len(pending)
        if len(pending) < report.threshold:
            report.status = STATUS_SKIPPED_THRESHOLD
        else:
            report.last_recorded = store.last_recorded_date()
            report.categories = clean_categories(store.category_options())
            log.info("Categories in force: %s", ", ".join(report.categories))
            extraction = extract(client, settings, system_prompt, pending, report.categories)
            report.input_tokens = extraction.input_tokens
            report.output_tokens = extraction.output_tokens
            report.cost_usd = extraction.cost_usd(settings)
            marks = _apply(extraction, pending, settings, report)
            report.processed = len(pending)
            if dry_run:
                report.status = STATUS_DRY_RUN
            else:
                if report.rows:
                    start, end = store.append_transactions(report.rows)
                    report.sheet_range = f"{settings.sheet_tab}!B{start}:G{end}"
                store.mark_messages(marks)
                report.status = STATUS_OK
    except Exception as exc:  # noqa: BLE001 - the report must be delivered whatever failed
        log.exception("Sync failed")
        report.status = STATUS_FAILED
        report.error = f"{type(exc).__name__}: {exc}"

    if not dry_run:
        try:
            store.log_run(report.as_row())
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not write the run log")
            report.log_error = f"{type(exc).__name__}: {exc}"
    return report


def _apply(
    extraction: Extraction,
    pending: list[InboxMessage],
    settings: Settings,
    report: RunReport,
) -> list[tuple[int, str, int, str]]:
    """Turn Claude's results into transaction rows and inbox status marks."""
    by_id = {message.message_id: message for message in pending}
    seen: set[int] = set()
    marks: list[tuple[int, str, int, str]] = []

    for result in extraction.results:
        message = by_id.get(result.message_id)
        if message is None or result.message_id in seen:
            log.warning("Ignoring result for unknown or repeated message id %s", result.message_id)
            continue
        seen.add(result.message_id)

        too_old: list[date] = []
        for transaction in result.transactions:
            when = parse_override_date(transaction.date) or rollover_date(
                message.sent_at, settings.day_rollover_hour
            )
            if report.last_recorded and when < report.last_recorded:
                too_old.append(when)  # the sheet is already complete up to last_recorded; a human decides
                continue
            report.rows.append(
                TransactionRow(
                    date=when,
                    amount=transaction.amount,
                    currency=transaction.currency,
                    description=sanitize_description(transaction.description),
                    category=transaction.category,
                    message_id=message.message_id,
                )
            )
        rows_added = len(result.transactions) - len(too_old)

        if too_old:
            note = (
                f"dated {', '.join(d.strftime('%d/%m/%Y') for d in too_old)}, before the sheet's last entry "
                f"({report.last_recorded:%d/%m/%Y}); not written, add by hand if it is new"
            )
            status = STATUS_NEEDS_REVIEW
            report.review.append(ReviewItem(message.message_id, message.sender, message.sent_at, message.text, note))
            marks.append((message.row, status, rows_added, note))
            continue

        if result.needs_review:
            status = STATUS_NEEDS_REVIEW
            report.review.append(
                ReviewItem(message.message_id, message.sender, message.sent_at, message.text, result.note or "needs a look")
            )
        elif rows_added == 0 and result.merged_into is not None:
            status = STATUS_MERGED
            report.merged += 1
        elif rows_added == 0:
            status = STATUS_SKIPPED
            report.skipped += 1
        else:
            status = STATUS_PROCESSED
        note = result.note or result.skip_reason or ""
        if result.merged_into is not None and rows_added == 0:
            note = f"merged into message {result.merged_into}" + (f": {note}" if note else "")
        marks.append((message.row, status, rows_added, note))

    for message in pending:
        if message.message_id not in seen:
            note = "Claude returned no result for this message"
            report.review.append(ReviewItem(message.message_id, message.sender, message.sent_at, message.text, note))
            marks.append((message.row, STATUS_NEEDS_REVIEW, 0, note))
    return marks


# ------------------------------------------------------------------ reporting


def format_amount(amount: float, currency: str) -> str:
    number = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    symbol = CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{number}" if symbol else f"{number} {currency}"


def format_totals(report: RunReport) -> str:
    return " · ".join(format_amount(total, currency) for currency, total in sorted(report.totals_by_currency().items()))


def format_summary(report: RunReport) -> str:
    """One or two lines for the group chat."""
    if report.status == STATUS_SKIPPED_THRESHOLD:
        if report.trigger == TRIGGER_SCHEDULE:
            return (
                f"⏭ Kashio: {report.pending} pending message(s), below the minimum of {report.threshold} "
                f"for a scheduled sync. Nothing was sent to Claude."
            )
        return f"Nothing new to process: {report.pending} pending message(s), minimum is {report.threshold}."
    if report.status == STATUS_FAILED:
        return f"⚠️ Kashio sync failed: {report.error}\nMessages stay pending and will be retried next time."
    prefix = "🧪 Dry run, nothing written." if report.status == STATUS_DRY_RUN else "✅ Kashio synced"
    parts = [f"{prefix} {len(report.rows)} expense(s) from {report.processed} message(s)"]
    if report.rows:
        parts[0] += f" ({format_totals(report)})"
    parts[0] += "."
    if report.skipped:
        parts.append(f"Skipped {report.skipped} non-expense message(s).")
    if report.merged:
        parts.append(f"{report.merged} message(s) merged into another.")
    if report.review:
        parts.append(f"{len(report.review)} need(s) a look, see the report.")
    return " ".join(parts)


def format_report(report: RunReport, include_rows: bool = False) -> str:
    """The full run record, mirrored to the admin chat so it survives even if the sheet is down."""
    lines = [
        "Kashio sync report",
        f"Status: {report.status}",
        f"Trigger: {report.trigger} ({report.requested_by})",
        f"Time: {report.started_at:%Y-%m-%d %H:%M} {report.started_at.tzname()}",
        f"Pending: {report.pending} · threshold {report.threshold} · processed {report.processed}",
        f"Rows added: {len(report.rows)}" + (f" → {report.sheet_range}" if report.sheet_range else ""),
    ]
    if report.rows:
        lines.append(f"Totals: {format_totals(report)}")
    lines.append(f"Skipped: {report.skipped} · merged: {report.merged} · needs review: {len(report.review)}")
    if report.last_recorded:
        lines.append(f"Sheet's last entry before this run: {report.last_recorded:%d/%m/%Y}")
    if report.input_tokens or report.output_tokens:
        lines.append(
            f"Tokens: {report.input_tokens:,} in / {report.output_tokens:,} out · "
            f"cost ${report.cost_usd:.4f} ({report.model}, effort {report.effort})"
        )
    if report.review:
        lines.append("Needs review:")
        for item in report.review[:MAX_REVIEW_ITEMS_IN_REPORT]:
            excerpt = " ".join(item.text.split())[:80]
            lines.append(f"• {item.sender} · {item.sent_at:%d %b %H:%M} · \"{excerpt}\" — {item.note}")
        if len(report.review) > MAX_REVIEW_ITEMS_IN_REPORT:
            lines.append(f"• … and {len(report.review) - MAX_REVIEW_ITEMS_IN_REPORT} more (see the inbox tab)")
    if include_rows and report.categories:
        lines.append("Categories: " + ", ".join(report.categories))
    if include_rows and report.rows:
        lines.append("Rows:")
        for row in report.rows:
            lines.append(
                f"• {row.date:%d/%m/%Y} · {format_amount(row.amount, row.currency)} · {row.currency} · "
                f"{row.description} · {row.category}"
            )
    if report.error:
        lines.append(f"Error: {report.error}")
    if report.log_error:
        lines.append(f"Run log: could not write to the runs tab ({report.log_error})")
    return "\n".join(lines)
