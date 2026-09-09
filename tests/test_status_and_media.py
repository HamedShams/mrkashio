"""Integration checklist wording, media detection, currency formats, and the sync orchestration. No network."""

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Chat, Document, Message, PhotoSize, User, Voice

import bot
import sync
from extractor import Extraction, result_model
from sheets import currency_format_requests
from tests.conftest import at


def stub_kashio(settings, store=None, store_error=None, claude=None, claude_error=None, config=None):
    app = bot.Kashio.__new__(bot.Kashio)
    app.settings = settings
    app.store, app.store_error = store, store_error
    app.claude, app.claude_error = claude, claude_error
    app._config = config or {}
    app._last_hint = {}
    return app


def test_status_names_every_missing_piece_with_its_fix(settings):
    settings = replace(settings, telegram_chat_id=None, telegram_admin_chat_id=None)
    app = stub_kashio(settings, store_error="GOOGLE_SERVICE_ACCOUNT_JSON is not set. Create a Google service account…",
                      claude_error="ANTHROPIC_API_KEY is not set. Create a key at console.anthropic.com…")
    text = app.status_text("mrkashio_bot")
    assert text.startswith("Not quite ready yet")
    assert "✅ Telegram: connected as @mrkashio_bot" in text
    assert "❌ Google Sheets: GOOGLE_SERVICE_ACCOUNT_JSON is not set" in text
    assert "❌ Claude: ANTHROPIC_API_KEY is not set" in text
    assert "⚠️ Not paired with a group yet" in text and "/setup" in text
    assert app.ready is False


def test_status_is_all_green_when_everything_is_connected(settings):
    store = SimpleNamespace(spreadsheet=SimpleNamespace(title="Budget"))
    app = stub_kashio(settings, store=store, claude=object(), config={"group_title": "Family"})
    text = app.status_text()
    assert text.startswith("All set") and "“Budget”" in text and "✅ Claude" in text and app.ready is True


def test_hints_are_rate_limited_per_chat(settings):
    app = stub_kashio(settings)
    assert app.hint_due(1) is True
    assert app.hint_due(1) is False
    assert app.hint_due(2) is True


def message_with(**fields):
    return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=-1, type=Chat.SUPERGROUP),
                   from_user=User(id=7, first_name="Alex", is_bot=False), **fields)


def test_media_kinds_are_described_without_touching_the_file():
    photo = message_with(photo=(PhotoSize(file_id="a", file_unique_id="b", width=1, height=1),))
    voice = message_with(voice=Voice(file_id="a", file_unique_id="b", duration=3))
    receipt = message_with(document=Document(file_id="a", file_unique_id="b", file_name="receipt.pdf"))
    assert bot.describe_media(photo) == "photo"
    assert bot.describe_media(voice) == "voice message"
    assert bot.describe_media(receipt) == "file receipt.pdf"
    assert bot.describe_media(message_with()) == "attachment"


def test_photo_without_caption_routes_to_the_media_handler_and_with_caption_to_ingest(settings):
    application = bot.build_application(SimpleNamespace(settings=settings))
    from telegram import Update
    from tests.test_bot import first_matching_handler
    photo = message_with(photo=(PhotoSize(file_id="a", file_unique_id="b", width=1, height=1),))
    assert first_matching_handler(application, Update(update_id=1, message=photo)).callback is bot.on_group_media
    captioned = message_with(photo=(PhotoSize(file_id="a", file_unique_id="b", width=1, height=1),), caption="Migros 450")
    assert first_matching_handler(application, Update(update_id=2, message=captioned)).callback is bot.on_group_message


def test_currency_formats_follow_column_d_and_group_runs():
    requests = currency_format_requests(sheet_id=9, start=153, currencies=["TRY", "TRY", "EUR", "TOMAN", "XXX"])
    ranges = [(r["repeatCell"]["range"]["startRowIndex"], r["repeatCell"]["range"]["endRowIndex"]) for r in requests]
    patterns = [r["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"]["pattern"] for r in requests]
    assert ranges == [(152, 154), (154, 155), (155, 156)]  # two TRY rows share one request; unknown currency untouched
    assert patterns == ["[$₺]#,##0.0", "[$€]#,##0.0", '#,##0 "TOMAN"']
    assert all(r["repeatCell"]["range"]["startColumnIndex"] == 2 for r in requests)


class FakeStore:
    """Just enough of SheetStore for run_sync."""

    def __init__(self, pending, categories=("Groceries", "Transport", "Other"), fail_write=False):
        self._pending, self._categories, self.fail_write = pending, list(categories), fail_write
        self.written, self.marks, self.runs = [], [], []

    def pending_messages(self):
        return list(self._pending)

    def last_recorded_date(self):
        return date(2026, 7, 29)

    def category_options(self):
        return self._categories

    def append_transactions(self, rows):
        if self.fail_write:
            raise RuntimeError("refusing to write: row 153 of 'Tab' already holds data; nothing was written")
        self.written.extend(rows)
        return 153, 152 + len(rows)

    def mark_messages(self, marks):
        self.marks.extend(marks)

    def log_run(self, values):
        self.runs.append(values)


def fake_extract(results):
    def _extract(client, settings, prompt, messages, categories):
        model = result_model(categories)
        return Extraction(model.model_validate({"results": results}).results, 1000, 100)
    return _extract


def test_run_sync_writes_marks_and_logs(settings, monkeypatch):
    from sheets import InboxMessage
    pending = [InboxMessage(2, 2, "Alex", at(2026, 9, 8, 12), None, "A101 300", "pending")]
    store = FakeStore(pending)
    monkeypatch.setattr(sync, "extract", fake_extract([{"message_id": 2, "transactions": [
        {"description": "Groceries - A101", "amount": 300, "currency": "TRY", "category": "Groceries", "date": None}],
        "merged_into": None, "skip_reason": None, "needs_review": False, "note": None}]))
    report = sync.run_sync(settings, store, object(), "prompt", trigger=sync.TRIGGER_MANUAL, requested_by="Alex")
    assert report.status == sync.STATUS_OK and report.sheet_range.endswith("!B153:G153")
    assert [r.description for r in store.written] == ["Groceries - A101"] and store.marks[0][1] == "processed"
    assert len(store.runs) == 1 and store.runs[0][3] == "ok" and report.cost_usd == pytest.approx(0.003)


def test_run_sync_respects_the_scheduled_threshold_and_reports_failures(settings, monkeypatch):
    from sheets import InboxMessage
    pending = [InboxMessage(2, 2, "Alex", at(2026, 9, 8, 12), None, "A101 300", "pending")]
    quiet = sync.run_sync(settings, FakeStore(pending), object(), "prompt", trigger=sync.TRIGGER_SCHEDULE, requested_by="schedule")
    assert quiet.status == sync.STATUS_SKIPPED_THRESHOLD and quiet.threshold == 5 and quiet.input_tokens == 0

    store = FakeStore(pending, fail_write=True)
    monkeypatch.setattr(sync, "extract", fake_extract([{"message_id": 2, "transactions": [
        {"description": "x", "amount": 1, "currency": "TRY", "category": "Other", "date": None}],
        "merged_into": None, "skip_reason": None, "needs_review": False, "note": None}]))
    failed = sync.run_sync(settings, store, object(), "prompt", trigger=sync.TRIGGER_MANUAL, requested_by="Alex")
    assert failed.status == sync.STATUS_FAILED and "refusing to write" in failed.error
    assert store.marks == [] and store.runs[0][3] == "failed"  # nothing marked, the failure is logged
