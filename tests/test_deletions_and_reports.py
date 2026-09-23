"""Deleted-message detection, free work below the threshold, Telegram HTML reports, and caption notes."""

import asyncio

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, PhotoSize, User
from telegram.error import BadRequest, RetryAfter

import bot
import sync
from extractor import Extraction
from sheets import STATUS_DELETED, STATUS_PENDING_DELETION, InboxMessage
from tests.conftest import at
from tests.test_status_and_media import FakeStore, fake_extract, stub_kashio


@pytest.fixture(autouse=True)
def _no_probe_pause(monkeypatch):
    monkeypatch.setattr(bot, "DELETION_PROBE_PAUSE", 0)


class ProbeBot:
    """Answers set_message_reaction the way Telegram does: existing → Reaction_empty, deleted → not found."""

    def __init__(self, existing, deleted, other=()):
        self.existing, self.deleted, self.other, self.probed = set(existing), set(deleted), set(other), []

    async def set_message_reaction(self, chat_id, message_id, reaction=None):
        self.probed.append(message_id)
        if message_id in self.deleted:
            raise BadRequest("Message to react not found")
        if message_id in self.other:
            raise BadRequest("Reactions are not available in this chat")
        raise BadRequest("Reaction_empty")


class ProbeStore:
    def __init__(self, candidates):
        self.candidates = [InboxMessage(row, mid, "Alex", at(2026, 9, 19, 14), None, "x", "processed", rows_added=1) for row, mid in candidates]
        self.marked = []

    def messages_with_rows(self):
        return list(self.candidates)

    def mark_deleted(self, messages):
        self.marked.extend(m.message_id for m in messages)


def test_deleted_messages_are_found_and_queued(settings):
    app = stub_kashio(settings, store=ProbeStore([(10, 73), (11, 78), (12, 61)]), claude=object())
    probe = ProbeBot(existing={73, 61}, deleted={78})
    assert asyncio.run(app.detect_deletions(probe)) == [78]
    assert probe.probed == [73, 78, 61] and app.store.marked == [78]


def test_a_reacted_to_message_still_counts_as_existing(settings):
    """People's reactions never matter: the probe only clears the bot's own, and any answer but 'not found' means the message exists."""

    class Reacted(ProbeBot):
        async def set_message_reaction(self, chat_id, message_id, reaction=None):
            self.probed.append(message_id)
            if message_id == 61:
                return True  # Telegram answered plainly, as it may for a message people reacted to
            return await super().set_message_reaction(chat_id, message_id, reaction)

    app = stub_kashio(settings, store=ProbeStore([(10, 73), (11, 78), (12, 61)]), claude=object())
    assert asyncio.run(app.detect_deletions(Reacted(existing={73}, deleted={78}))) == [78]


def test_flood_control_is_waited_out_and_old_messages_are_not_probed(settings, monkeypatch):
    monkeypatch.setattr(bot.asyncio, "sleep", _no_sleep)

    class Flooded(ProbeBot):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.flooded = False

        async def set_message_reaction(self, chat_id, message_id, reaction=None):
            if message_id == 78 and not self.flooded:
                self.flooded = True
                self.probed.append(message_id)
                raise RetryAfter(3)
            return await super().set_message_reaction(chat_id, message_id, reaction)

    store = ProbeStore([(10, 73), (11, 78), (12, 61)])
    store.candidates[2].sent_at = at(2026, 1, 1)  # far older than DELETION_PROBE_DAYS: left alone
    app = stub_kashio(settings, store=store, claude=object())
    probe = Flooded(existing={73}, deleted={78})
    assert asyncio.run(app.detect_deletions(probe)) == [78]
    assert probe.probed == [73, 78, 78]  # 78 probed again after the pause, 61 never


async def _no_sleep(_seconds):
    return None


def test_unexpected_answers_are_not_deletions_and_a_total_wipe_is_refused(settings):
    app = stub_kashio(settings, store=ProbeStore([(10, 73), (11, 78)]), claude=object())
    assert asyncio.run(app.detect_deletions(ProbeBot(existing=set(), deleted=set(), other={73, 78}))) == []
    app = stub_kashio(settings, store=ProbeStore([(10, 1), (11, 2), (12, 3)]), claude=object())
    assert asyncio.run(app.detect_deletions(ProbeBot(existing=set(), deleted={1, 2, 3}))) == []  # everything "deleted": abort
    assert app.store.marked == []


def test_deletion_removes_rows_even_below_the_scheduled_threshold(settings, monkeypatch):
    pending = [InboxMessage(2, 78, "Alex", at(2026, 9, 19, 14), None, "$10 havale", STATUS_PENDING_DELETION, rows_added=1)]
    store = FakeStore(pending)
    monkeypatch.setattr(sync, "extract", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Claude must not be called")))
    report = sync.run_sync(settings, store, object(), "prompt", trigger=sync.TRIGGER_SCHEDULE, requested_by="schedule")
    assert report.status == sync.STATUS_OK and report.calls == 0
    assert store.replaced == [(78, [])] and report.rows_deleted == 1
    assert store.marks == [(pending[0], STATUS_DELETED, 0, "deleted in Telegram: 1 rows removed from the sheet")]
    summary = sync.format_summary(report)
    assert "made no Claude call" in summary and "Removed 1 rows (1 messages deleted in Telegram)" in summary


def test_text_messages_still_wait_for_the_threshold_while_deletions_go_through(settings, monkeypatch):
    pending = [InboxMessage(2, 78, "Alex", at(2026, 9, 19, 14), None, "$10 havale", STATUS_PENDING_DELETION, rows_added=1),
               InboxMessage(3, 80, "Alex", at(2026, 9, 19, 15), None, "A101 300", "pending")]
    store = FakeStore(pending)
    monkeypatch.setattr(sync, "extract", lambda *a, **k: (_ for _ in ()).throw(AssertionError("below threshold: no call")))
    report = sync.run_sync(settings, store, object(), "prompt", trigger=sync.TRIGGER_SCHEDULE, requested_by="schedule")
    assert report.status == sync.STATUS_OK and report.rows_deleted == 1 and [m[0].message_id for m in store.marks] == [78]
    assert "1 pending messages, below the minimum of 5" in sync.format_summary(report)


def test_html_report_is_bold_bulleted_and_escaped(settings):
    report = sync.RunReport("manual", "Alex <3", at(2026, 9, 19, 14, 9), "claude-sonnet-5", "high", status=sync.STATUS_OK, pending=8, processed=8, calls=1)
    report.review.append(sync.ReviewItem(52, "Alex", at(2026, 9, 16, 17, 4), "Sep 3 & more", "amount '11,58.4' <unclear>"))
    text = sync.format_report(report, as_html=True)
    lines = text.split("\n")
    assert lines[0] == "<b>💸 Kashio sync report</b>" and lines[1].startswith("• Status: ok")
    assert "• Trigger: manual (Alex &lt;3)" in text and "<b>⚠️ Needs review:</b>" in text
    assert "Sep 3 &amp; more" in text and "&lt;unclear&gt;" in text and "<unclear>" not in text
    plain = sync.format_report(report)
    assert plain.startswith("💸 Kashio sync report\n• Status: ok") and "<b>" not in plain and "<unclear>" in plain


def test_photo_with_caption_stores_the_caption_and_notes_the_attachment(settings):
    calls = []
    store = SimpleNamespace(add_message=lambda *args: calls.append(args), add_skipped=lambda *args: calls.append(("skipped",) + args))
    app = stub_kashio(settings, store=store, claude=object())
    message = Message(message_id=9, date=datetime.now(timezone.utc), chat=Chat(id=settings.telegram_chat_id, type=Chat.SUPERGROUP),
                      from_user=User(id=7, first_name="Alex", is_bot=False), caption="Migros 450",
                      photo=(PhotoSize(file_id="a", file_unique_id="b", width=1, height=1),))
    update = SimpleNamespace(effective_message=message, effective_chat=message.chat)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"kashio": app}))
    asyncio.run(bot.on_group_message(update, context))
    assert len(calls) == 1 and calls[0][3] == "Migros 450" and "photo" in calls[0][4] and "not downloaded" in calls[0][4]
