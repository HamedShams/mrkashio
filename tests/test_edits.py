"""What an edit does to the inbox: the old row is superseded, a new row carries the edited text and the next action."""

from types import SimpleNamespace

from sheets import STATUS_PENDING, STATUS_PENDING_REVISION, STATUS_SKIPPED, STATUS_SUPERSEDED, SheetStore
from tests.conftest import at

HEADER = ["message_id", "sender", "sent_at", "edited_at", "text", "status", "processed_at", "rows_added", "note"]


class StubInbox:
    def __init__(self, *rows):
        self.rows, self.updates, self.appended = [HEADER, *rows], [], []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def batch_update(self, updates):
        self.updates.extend(updates)

    def append_row(self, values, value_input_option=None):
        self.appended.append(values)


def store_with(settings, *rows):
    store = SheetStore.__new__(SheetStore)
    store.settings, store.columns = settings, settings.columns
    store.inbox = StubInbox(*rows)
    return store


def superseded_row(inbox):
    return next(int(u["range"][1:]) for u in inbox.updates if u["values"][0][0] == STATUS_SUPERSEDED)


def test_pending_message_gets_a_new_pending_row_and_the_old_one_is_superseded(settings):
    store = store_with(settings, ["42", "Alex", "2026-09-08 12:00:00", "", "A101", STATUS_PENDING, "", "", ""])
    assert store.update_message(42, "A101 300", at(2026, 9, 8, 12, 5)) == STATUS_PENDING
    assert superseded_row(store.inbox) == 2
    new = store.inbox.appended[0]
    assert new[0] == "42" and new[4] == "A101 300" and new[5] == STATUS_PENDING and new[3] == "2026-09-08 12:05:00"
    assert new[1] == "Alex" and new[2] == "2026-09-08 12:00:00"  # sender and original send time carried over


def test_the_latest_row_is_used_when_a_message_was_edited_before(settings):
    store = store_with(settings,
                       ["42", "Alex", "2026-09-08 12:00:00", "", "A101", STATUS_SUPERSEDED, "", "", "superseded"],
                       ["42", "Alex", "2026-09-08 12:00:00", "2026-09-08 12:05:00", "A101 300", "processed", "2026-09-08 13:00:00", "1", ""])
    assert store.update_message(42, "A101 350", at(2026, 9, 8, 14)) == STATUS_PENDING_REVISION
    assert superseded_row(store.inbox) == 3 and store.inbox.appended[0][7] == 1  # rows_added carried for the hold rule


def test_skipped_message_without_rows_is_reopened(settings):
    store = store_with(settings, ["42", "Alex", "2026-09-08 12:00:00", "", "IKEA", STATUS_SKIPPED, "2026-09-08 13:00:00", "0", "no amount"])
    assert store.update_message(42, "IKEA 900 TL", at(2026, 9, 8, 14)) == STATUS_PENDING
    assert store.inbox.appended[0][8] == "reopened after an edit"


def test_message_with_rows_edited_into_a_note_is_a_retraction(settings):
    store = store_with(settings, ["42", "Alex", "2026-09-08 12:00:00", "", "A101 300", "processed", "2026-09-08 13:00:00", "1", ""])
    assert store.update_message(42, "[note]", at(2026, 9, 8, 14), retire_reason="private note") == STATUS_PENDING_REVISION
    assert store.inbox.appended[0][4] == "[note]" and "removed" in store.inbox.appended[0][8]


def test_pending_message_edited_into_a_note_is_closed(settings):
    store = store_with(settings, ["42", "Alex", "2026-09-08 12:00:00", "", "A101 300", STATUS_PENDING, "", "", ""])
    assert store.update_message(42, "[note]", at(2026, 9, 8, 14), retire_reason="private note") == STATUS_SKIPPED


def test_unknown_message_returns_none(settings):
    store = store_with(settings, ["1", "Alex", "2026-09-08 12:00:00", "", "x", STATUS_PENDING, "", "", ""])
    assert store.update_message(999, "y", at(2026, 9, 8, 14)) is None and store.inbox.appended == []
