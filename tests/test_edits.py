"""What an edit does to a stored message, driven through a stubbed inbox tab."""

from datetime import datetime
from types import SimpleNamespace

from sheets import STATUS_PENDING, STATUS_PENDING_REVISION, STATUS_SKIPPED, SheetStore
from tests.conftest import at


class StubInbox:
    def __init__(self, row):
        self.row, self.updates = row, []

    def find(self, query, in_column=None):
        return SimpleNamespace(row=7) if query == self.row[0] else None

    def row_values(self, index):
        return list(self.row)

    def batch_update(self, updates):
        self.updates.extend(updates)


def store_with(row, settings):
    store = SheetStore.__new__(SheetStore)
    store.settings = settings
    store.columns = settings.columns
    store.inbox = StubInbox(row)
    return store


def status_written(inbox):
    for update in inbox.updates:
        if update["range"].startswith("F7"):
            return update["values"][0][0]
    return None


def test_pending_message_keeps_pending_and_gets_the_new_text(settings):
    store = store_with(["42", "Alex", "2026-09-08 12:00:00", "", "A101", STATUS_PENDING, "", "", ""], settings)
    assert store.update_message(42, "A101 300", at(2026, 9, 8, 12, 5)) == STATUS_PENDING
    assert store.inbox.updates[0]["values"][0][1] == "A101 300" and status_written(store.inbox) is None


def test_skipped_message_without_rows_is_reopened(settings):
    store = store_with(["42", "Alex", "2026-09-08 12:00:00", "", "IKEA", STATUS_SKIPPED, "2026-09-08 13:00:00", "0", "no amount"], settings)
    assert store.update_message(42, "IKEA 900 TL", at(2026, 9, 8, 14)) == STATUS_PENDING
    assert status_written(store.inbox) == STATUS_PENDING


def test_message_that_produced_rows_becomes_a_revision(settings):
    store = store_with(["42", "Alex", "2026-09-08 12:00:00", "", "A101 300", "processed", "2026-09-08 13:00:00", "1", ""], settings)
    assert store.update_message(42, "A101 350", at(2026, 9, 8, 14)) == STATUS_PENDING_REVISION
    assert status_written(store.inbox) == STATUS_PENDING_REVISION


def test_message_with_rows_edited_into_a_note_is_a_retraction(settings):
    store = store_with(["42", "Alex", "2026-09-08 12:00:00", "", "A101 300", "processed", "2026-09-08 13:00:00", "1", ""], settings)
    assert store.update_message(42, "[note]", at(2026, 9, 8, 14), retire_reason="private note") == STATUS_PENDING_REVISION
    assert store.inbox.updates[0]["values"][0][1] == "[note]"


def test_pending_message_edited_into_a_note_is_retired(settings):
    store = store_with(["42", "Alex", "2026-09-08 12:00:00", "", "A101 300", STATUS_PENDING, "", "", ""], settings)
    assert store.update_message(42, "[note]", at(2026, 9, 8, 14), retire_reason="private note") == STATUS_SKIPPED
    assert status_written(store.inbox) == STATUS_SKIPPED


def test_unknown_message_returns_none(settings):
    store = store_with(["1", "Alex", "2026-09-08 12:00:00", "", "x", STATUS_PENDING, "", "", ""], settings)
    assert store.update_message(999, "y", at(2026, 9, 8, 14)) is None
