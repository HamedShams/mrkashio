"""/review: list what waits for a person, queue an item anyway, or close it. Every action is a new inbox row."""

import asyncio
from types import SimpleNamespace

from telegram import Chat

import bot
from sheets import STATUS_DUPLICATE, STATUS_NEEDS_REVIEW, STATUS_PENDING, STATUS_PENDING_REVISION, STATUS_RESOLVED, InboxMessage
from tests.conftest import at
from tests.test_status_and_media import stub_kashio


class ReviewStore:
    def __init__(self, items):
        self.items, self.marks = items, []

    def review_items(self):
        return list(self.items)

    def mark_messages(self, marks):
        self.marks.extend(marks)


def run(app, text, chat_data, sent):
    message = SimpleNamespace(text=text, reply_text=lambda t, **k: _record(sent, t), chat_id=42)
    chat = SimpleNamespace(id=42, type=Chat.PRIVATE)
    update = SimpleNamespace(effective_chat=chat, effective_message=message, effective_user=SimpleNamespace(id=1, first_name="Alex"))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"kashio": app}), chat_data=chat_data,
                              bot=SimpleNamespace(username="kashio_bot", send_message=lambda chat_id, text, parse_mode=None: _record(sent, text)))
    asyncio.run(bot.on_review(update, context))


async def _record(sent, text):
    sent.append(text)


def app_with(settings, items):
    app = stub_kashio(settings, store=ReviewStore(items), claude=object())
    app.is_household_member = lambda bot, user: _yes()
    return app


async def _yes():
    return True


ITEMS = [
    InboxMessage(5, -7, "Alex", at(2026, 9, 3, 9, 59), None, "UBER to Metro Station\n84 TL", STATUS_DUPLICATE,
                 note="looks already in the sheet (row 161: 03/09/2026 · UBER to Metro Station · ₺84)"),
    InboxMessage(8, 50, "Sam", at(2026, 9, 8, 12), None, "Cafe", STATUS_NEEDS_REVIEW, note="no amount given"),
    InboxMessage(9, 52, "Sam", at(2026, 9, 16, 17), None, "Sep 3 dump <x>", STATUS_NEEDS_REVIEW, rows_added=12, note="rows left unchanged"),
]


def test_review_lists_numbered_items_and_remembers_them(settings):
    sent, chat_data = [], {}
    run(app_with(settings, ITEMS), "/review", chat_data, sent)
    text = sent[0]
    assert text.startswith("<b>🔎 3 item(s) waiting for a look</b>")
    assert "1. Alex · 03 Sep 09:59 · “UBER to Metro Station | 84 TL” — possible duplicate: looks already in the sheet" in text
    assert "2. Sam · 08 Sep 12:00 · “Cafe” — needs review: no amount given" in text and "&lt;x&gt;" in text
    assert chat_data["review"] == ITEMS


def test_keep_queues_the_chosen_items_and_done_closes_them(settings):
    sent, chat_data = [], {}
    app = app_with(settings, ITEMS)
    run(app, "/review", chat_data, sent)
    run(app, "/review keep 1 3", chat_data, sent)
    assert [(m.message_id, status, rows) for m, status, rows, _ in app.store.marks] == [(-7, STATUS_PENDING, ""), (52, STATUS_PENDING_REVISION, 12)]
    assert app.store.marks[0][3] == "queued again through /review by Alex" and "Queued 2 message(s)" in sent[-1]
    assert chat_data["review"] == [ITEMS[1]]  # the rest stays addressable
    run(app, "/review done all", chat_data, sent)
    assert app.store.marks[-1][:2] == (ITEMS[1], STATUS_RESOLVED) and "Closed 1 item(s)" in sent[-1] and chat_data["review"] == []


def test_actions_need_a_list_first_and_valid_numbers(settings):
    sent, chat_data = [], {}
    app = app_with(settings, ITEMS)
    run(app, "/review done 1", chat_data, sent)
    assert "Send /review first" in sent[-1] and app.store.marks == []
    run(app, "/review", chat_data, sent)
    run(app, "/review done 9", chat_data, sent)
    assert "Say which ones" in sent[-1] and app.store.marks == []


def test_empty_review_says_so(settings):
    sent = []
    run(app_with(settings, []), "/review", {}, sent)
    assert sent == ["Nothing waits for a look."]


def test_review_is_a_command(settings):
    from tests.test_bot import first_matching_handler, update_for
    application = bot.build_application(SimpleNamespace(settings=settings))
    assert first_matching_handler(application, update_for("/review keep 1", chat_type=Chat.PRIVATE)).callback is bot.on_review
