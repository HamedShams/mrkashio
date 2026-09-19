"""One sync run from start to finish: threshold check, Claude, row building, sheet writes, report.

The report is built in memory first and survives any failure, so the Telegram notification always goes
out even when the spreadsheet is unreachable. It says exactly what was written, what was not, and why.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import anthropic

from config import Settings
from extractor import Extraction, clean_categories, extract
from sheets import (
    STATUS_DELETED,
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
MAX_REVIEW_ITEMS_IN_SUMMARY = 5
NOTE_ONLY_TEXT = "[note]"


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
    rows: list[TransactionRow] = field(default_factory=list)  # new rows appended for fresh messages
    sheet_range: str = ""
    skipped: int = 0
    merged: int = 0  # messages folded into another message's transaction
    review: list[ReviewItem] = field(default_factory=list)
    revisions: list[tuple[InboxMessage, list[TransactionRow]]] = field(default_factory=list)  # edited messages and their new rows
    retractions: list[InboxMessage] = field(default_factory=list)  # edited into a note: rows to remove
    deletions: list[InboxMessage] = field(default_factory=list)  # deleted in Telegram: rows to remove
    rows_updated: int = 0
    rows_deleted: int = 0
    unanswered: list[int] = field(default_factory=list)  # message ids Claude gave no usable answer for; they stay pending
    held: list[ReviewItem] = field(default_factory=list)  # edited messages whose rows were left untouched because the new answer looked incomplete
    problems: list[str] = field(default_factory=list)  # what was wrong with rejected answers
    suspicious: dict[int, str] = field(default_factory=dict)  # message id → why its answer looks cut short
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    categories: list[str] = field(default_factory=list)  # the category names in force for this run
    error: str | None = None
    log_error: str | None = None  # set when writing the run log itself failed

    @property
    def revised(self) -> int:
        return len(self.revisions) + len(self.retractions)

    @property
    def removed_messages(self) -> int:
        return len(self.retractions) + len(self.deletions)

    def totals_by_currency(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.rows:
            totals[row.currency] = totals.get(row.currency, 0.0) + row.amount
        return totals

    def as_row(self) -> list[object]:
        """Values in the order of sheets.RUNS_HEADERS."""
        return [
            self.started_at.strftime("%Y-%m-%d %H:%M:%S"), self.trigger, self.requested_by, self.status,
            self.pending, self.processed, len(self.rows), self.sheet_range, self.skipped, len(self.review),
            self.input_tokens, self.output_tokens, round(self.cost_usd, 4), self.model, self.effort, self.error or "",
            self.merged, len(self.unanswered), self.revised, self.rows_updated, self.rows_deleted, self.calls,
        ]


def split_note(text: str, keyword: str) -> tuple[str, bool]:
    """Cut a private note off a message: everything from the keyword (a whole word, any case) to the end.

    Returns (what is left, whether a note was present). "A101 300 #note oil for the week" → ("A101 300", True);
    "#note review the budget on Friday" → ("", True). The note itself is never stored anywhere.
    """
    if not keyword:
        return text, False
    match = re.search(r"(?<!\S)" + re.escape(keyword) + r"(?!\w)", text, re.IGNORECASE)
    if not match:
        return text, False
    return text[: match.start()].rstrip(), True


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
        trigger=trigger, requested_by=requested_by, started_at=datetime.now(settings.timezone),
        model=settings.anthropic_model, effort=settings.anthropic_effort,
    )
    report.threshold = settings.scheduled_min_messages if trigger == TRIGGER_SCHEDULE else settings.manual_min_messages
    try:
        pending = store.pending_messages()
        report.pending = len(pending)
        # Deletions and notes-only edits cost nothing and are applied regardless of the threshold; only Claude is gated.
        report.deletions = [m for m in pending if m.deletion]
        report.retractions = [m for m in pending if m.revision and m.text.strip() in ("", NOTE_ONLY_TEXT)]
        to_extract = [m for m in pending if not m.deletion and m not in report.retractions]
        ask_claude = len(to_extract) >= report.threshold and bool(to_extract)
        if not ask_claude and not report.deletions and not report.retractions:
            report.status = STATUS_SKIPPED_THRESHOLD
        else:
            extraction = Extraction(results=[])
            marks: list[tuple[int, str, int, str]] = []
            if ask_claude:
                report.categories = clean_categories(store.category_options())
                log.info("Categories in force: %s", ", ".join(report.categories))
                extraction = extract(client, settings, system_prompt, to_extract, report.categories)
                report.calls, report.input_tokens, report.output_tokens = extraction.calls, extraction.input_tokens, extraction.output_tokens
                report.cost_usd = extraction.cost_usd(settings)
                report.unanswered = list(extraction.unanswered)
                report.problems = list(extraction.problems)
                report.suspicious = dict(extraction.suspicious)
                marks = _apply(extraction, to_extract, settings, report)
                report.processed = len(to_extract) - len(report.unanswered)
            else:
                report.unanswered = []  # nothing was asked; the text messages simply wait for the threshold
            if dry_run:
                report.status = STATUS_DRY_RUN
            else:
                if report.rows:
                    start, end = store.append_transactions(report.rows)
                    report.sheet_range = f"{settings.sheet_tab}!{settings.columns.date}{start}:{settings.columns.category}{end}"
                for message, rows in report.revisions:
                    outcome = store.replace_transactions(message.message_id, rows)
                    report.rows_updated += outcome.updated
                    report.rows_deleted += outcome.deleted
                    marks.append((message.row, STATUS_PROCESSED if rows else STATUS_SKIPPED, len(rows),
                                  f"re-synced after an edit: {outcome.updated} updated, {outcome.deleted} removed, {outcome.appended} added"))
                for message in report.retractions:
                    outcome = store.replace_transactions(message.message_id, [])
                    report.rows_deleted += outcome.deleted
                    marks.append((message.row, STATUS_SKIPPED, 0, f"retracted after an edit: {outcome.deleted} row(s) removed"))
                for message in report.deletions:
                    outcome = store.replace_transactions(message.message_id, [])
                    report.rows_deleted += outcome.deleted
                    marks.append((message.row, STATUS_DELETED, 0, f"deleted in Telegram: {outcome.deleted} row(s) removed from the sheet"))
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
    """Turn Claude's results into transaction rows and inbox status marks. Unanswered messages get no mark: they stay pending."""
    by_id = {message.message_id: message for message in pending}
    marks: list[tuple[int, str, int, str]] = []
    for result in extraction.results:
        message = by_id.get(result.message_id)
        if message is None:
            continue  # audited away already; belt and braces
        rows = [
            TransactionRow(
                date=parse_override_date(t.date) or rollover_date(message.sent_at, settings.day_rollover_hour),
                amount=t.amount, currency=t.currency, description=sanitize_description(t.description),
                category=t.category, message_id=message.message_id,
            )
            for t in result.transactions
        ]
        note = result.note or result.skip_reason or ""
        short = report.suspicious.get(message.message_id)
        if message.revision:
            hold = _hold_reason(message, rows, result, short)
            if hold:
                # Never let a doubtful re-extraction delete rows: keep the sheet as it is and ask the human.
                report.held.append(ReviewItem(message.message_id, message.sender, message.sent_at, message.text, hold))
                marks.append((message.row, STATUS_NEEDS_REVIEW, message.rows_added, f"rows left unchanged: {hold}; edit the message again to retry"))
                continue
            report.revisions.append((message, rows))  # marked after the sheet has been updated
            continue
        if short:
            result.needs_review = True
            note = f"answer may be incomplete: {short}" + (f"; {note}" if note else "")
        if result.needs_review:
            report.review.append(ReviewItem(message.message_id, message.sender, message.sent_at, message.text, result.note or note or "needs a look"))
        report.rows.extend(rows)
        if result.needs_review:
            status = STATUS_NEEDS_REVIEW
        elif not rows and result.merged_into is not None:
            status = STATUS_MERGED
            report.merged += 1
            note = f"merged into message {result.merged_into}" + (f": {note}" if note else "")
        elif not rows:
            status = STATUS_SKIPPED
            report.skipped += 1
        else:
            status = STATUS_PROCESSED
        marks.append((message.row, status, len(rows), note))
    return marks


def _hold_reason(message: InboxMessage, rows: list[TransactionRow], result, short: str | None) -> str | None:
    """Why an edited message's rows must not be replaced yet. None means the replacement is safe."""
    if short:
        return f"the new answer looks cut short ({short})"
    if result.needs_review and len(rows) < message.rows_added:
        return f"Claude was unsure ({result.note or 'needs a look'}) and returned fewer items ({len(rows)}) than the {message.rows_added} rows already written"
    if message.rows_added >= 2 and len(rows) * 2 < message.rows_added and not result.skip_reason:
        return f"the new answer has {len(rows)} item(s) for a message that had {message.rows_added} rows"
    return None


# ------------------------------------------------------------------ reporting


def format_amount(amount: float, currency: str) -> str:
    number = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    symbol = CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{number}" if symbol else f"{number} {currency}"


def format_totals(report: RunReport) -> str:
    return " · ".join(format_amount(total, currency) for currency, total in sorted(report.totals_by_currency().items()))


STATUS_EMOJI = {STATUS_OK: "💸", STATUS_DRY_RUN: "🧪", STATUS_SKIPPED_THRESHOLD: "⏭", STATUS_FAILED: "❌"}


def _t(value: object, as_html: bool) -> str:
    """User-supplied text (descriptions, notes, errors) escaped for Telegram HTML when needed."""
    return html.escape(str(value), quote=False) if as_html else str(value)  # Telegram needs only <, > and & escaped


def _excerpt(item: ReviewItem, as_html: bool = False) -> str:
    text = " | ".join(part.strip() for part in item.text.splitlines() if part.strip())[:60]
    return _t(f"{item.sender} · {item.sent_at:%d %b %H:%M} · \"{text}\"", as_html)


def _below_threshold_line(report: RunReport) -> str:
    if report.trigger == TRIGGER_SCHEDULE:
        return (f"⏭ {report.pending - report.removed_messages} pending message(s), below the minimum of {report.threshold} "
                f"for a scheduled sync. Nothing was sent to Claude.")
    return f"Nothing new to process: {report.pending - report.removed_messages} pending message(s), minimum is {report.threshold}."


def format_summary(report: RunReport, as_html: bool = False) -> str:
    """What happened, in the group. Every message that was not written is accounted for."""
    if report.status == STATUS_SKIPPED_THRESHOLD:
        return _below_threshold_line(report)
    if report.status == STATUS_FAILED:
        return f"❌ Kashio sync failed: {_t(report.error, as_html)}\nMessages stay pending and will be retried next time."
    lines = []
    prefix = "🧪 Dry run, nothing written." if report.status == STATUS_DRY_RUN else "✅ Kashio"
    written = len(report.rows) + report.rows_updated
    if written:
        lines.append(f"{prefix} wrote {len(report.rows)} new row(s)" + (f" ({format_totals(report)})" if report.rows else "")
                     + (f" and updated {report.rows_updated}" if report.rows_updated else "") + f" from {report.processed} message(s).")
    elif report.calls:
        lines.append(f"{prefix} wrote nothing from {report.processed} message(s).")
    else:
        lines.append(f"{prefix} made no Claude call this time.")
    if report.rows_deleted:
        what = []
        if report.deletions:
            what.append(f"{len(report.deletions)} message(s) deleted in Telegram")
        if report.retractions:
            what.append(f"{len(report.retractions)} retracted by an edit")
        lines.append(f"🗑 Removed {report.rows_deleted} row(s) ({', '.join(what) or 'edited messages'}).")
    if not report.calls and report.pending - report.removed_messages:
        lines.append(_below_threshold_line(report))
    if report.skipped:
        lines.append(f"⏭ Skipped {report.skipped} message(s) that were not expenses.")
    if report.merged:
        lines.append(f"↩️ {report.merged} message(s) merged into another (an amount or a correction).")
    if report.review:
        lines.append(f"⚠️ {len(report.review)} message(s) need a look:")
        for item in report.review[:MAX_REVIEW_ITEMS_IN_SUMMARY]:
            lines.append(f"  • {_excerpt(item, as_html)} — {_t(item.note, as_html)}")
        if len(report.review) > MAX_REVIEW_ITEMS_IN_SUMMARY:
            lines.append(f"  • … and {len(report.review) - MAX_REVIEW_ITEMS_IN_SUMMARY} more, see the report")
    if report.held:
        lines.append(f"✋ {len(report.held)} edited message(s) left unchanged in the sheet, because the new answer looked incomplete:")
        for item in report.held[:MAX_REVIEW_ITEMS_IN_SUMMARY]:
            lines.append(f"  • {_excerpt(item, as_html)} — {_t(item.note, as_html)}. Edit it again to retry.")
    if report.unanswered:
        lines.append(f"🔁 {len(report.unanswered)} message(s) got no usable answer from Claude and stay pending; "
                     "they will be retried at the next sync.")
    return "\n".join(lines)


def format_report(report: RunReport, include_rows: bool = False, as_html: bool = False) -> str:
    """The full run record, mirrored to the admin chat so it survives even if the sheet is down.

    Plain text for the terminal; Telegram HTML (bold header, escaped content) for the private chat.
    """
    def bold(text: str) -> str:
        return f"<b>{text}</b>" if as_html else text

    head = f"{STATUS_EMOJI.get(report.status, '•')} Kashio sync report"
    lines = [
        bold(head),
        f"• Status: {report.status}",
        f"• Trigger: {report.trigger} ({_t(report.requested_by, as_html)})",
        f"• Time: {report.started_at:%Y-%m-%d %H:%M} {report.started_at.tzname()}",
        f"• Pending: {report.pending} · threshold {report.threshold} · answered {report.processed}",
        f"• New rows: {len(report.rows)}" + (f" → {_t(report.sheet_range, as_html)}" if report.sheet_range else ""),
    ]
    if report.rows:
        lines.append(f"• Totals: {format_totals(report)}")
    if report.revised or report.deletions:
        lines.append(f"• Edited messages re-synced: {report.revised} · deleted in Telegram: {len(report.deletions)} · "
                     f"rows updated {report.rows_updated} · rows removed {report.rows_deleted}")
    lines.append(f"• Skipped: {report.skipped} · merged: {report.merged} · needs review: {len(report.review)} · "
                 f"held: {len(report.held)} · unanswered: {len(report.unanswered)}")
    if report.calls or report.input_tokens:
        lines.append(f"• Claude: {report.calls} call(s), {report.input_tokens:,} in / {report.output_tokens:,} out · "
                     f"cost ${report.cost_usd:.4f} ({report.model}, effort {report.effort})")
    if report.problems:
        lines += ["", bold("🚫 Rejected answers:")]
        lines += [f"• {_t(problem, as_html)}" for problem in report.problems[:4]]
        if len(report.problems) > 4:
            lines.append(f"• … and {len(report.problems) - 4} more")
    if report.unanswered:
        lines += ["", bold("🔁 Still pending, no usable answer (retried next sync):"),
                  f"• message ids {', '.join(map(str, report.unanswered))}"]
    if report.held:
        lines += ["", bold("✋ Edited messages held (sheet unchanged):")]
        lines += [f"• {_excerpt(item, as_html)} — {_t(item.note, as_html)}" for item in report.held]
    if report.review:
        lines += ["", bold("⚠️ Needs review:")]
        lines += [f"• {_excerpt(item, as_html)} — {_t(item.note, as_html)}" for item in report.review[:MAX_REVIEW_ITEMS_IN_REPORT]]
        if len(report.review) > MAX_REVIEW_ITEMS_IN_REPORT:
            lines.append(f"• … and {len(report.review) - MAX_REVIEW_ITEMS_IN_REPORT} more (see the inbox tab)")
    if include_rows and report.categories:
        lines += ["", bold("Categories: ") + _t(", ".join(report.categories), as_html)]
    if include_rows:
        for row in report.rows:
            lines.append(f"• {row.date:%d/%m/%Y} · {format_amount(row.amount, row.currency)} · {row.currency} · "
                         f"{_t(row.description, as_html)} · {_t(row.category, as_html)}")
        for message, rows in report.revisions:
            lines.append(f"• message {message.message_id} re-synced → {len(rows)} row(s):")
            for row in rows:
                lines.append(f"    {row.date:%d/%m/%Y} · {format_amount(row.amount, row.currency)} · {_t(row.description, as_html)} · {_t(row.category, as_html)}")
    if report.error:
        lines += ["", bold("❌ Error: ") + _t(report.error, as_html)]
    if report.log_error:
        lines.append(f"• Run log: could not write to the runs tab ({_t(report.log_error, as_html)})")
    return "\n".join(lines)
