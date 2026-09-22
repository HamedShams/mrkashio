"""The inbox is a log: an edit, a sync mark or a deletion appends a row; nothing there is ever updated or deleted."""

from sheets import (
    STATUS_DUPLICATE,
    STATUS_NEEDS_REVIEW,
    STATUS_PENDING,
    STATUS_PENDING_DELETION,
    STATUS_PENDING_REVISION,
    STATUS_PROCESSED,
    STATUS_RESOLVED,
    STATUS_SKIPPED,
    InboxMessage,
    SheetStore,
)
from tests.conftest import at

HEADER = ["message_id", "sender", "sent_at", "edited_at", "text", "status", "logged_at", "rows_added", "note"]


class StubInbox:
    """Only append_rows is allowed: any update or delete would fail the test."""

    def __init__(self, *rows):
        self.rows, self.appended = [HEADER, *rows], []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def col_values(self, index):
        return [r[index - 1] for r in self.rows]

    def append_rows(self, values, value_input_option=None):
        self.appended.extend(values)
        self.rows.extend(values)

    def __getattr__(self, name):  # batch_update, update, update_cell, delete_rows, clear …
        raise AssertionError(f"the inbox must only be appended to; {name} was called")


def store_with(settings, *rows):
    store = SheetStore.__new__(SheetStore)
    store.settings, store.columns = settings, settings.columns
    store.inbox = StubInbox(*rows)
    return store


def row(mid, text, status, rows_added="", note="", edited=""):
    return [str(mid), "Alex", "2026-09-08 12:00:00", edited, text, status, "2026-09-08 12:00:01", rows_added, note]


def test_pending_message_edit_appends_a_pending_row_and_touches_nothing(settings):
    store = store_with(settings, row(42, "A101", STATUS_PENDING))
    assert store.update_message(42, "A101 300", at(2026, 9, 8, 12, 5)) == STATUS_PENDING
    assert len(store.inbox.appended) == 1
    new = store.inbox.appended[0]
    assert new[0] == "42" and new[4] == "A101 300" and new[5] == STATUS_PENDING and new[3] == "2026-09-08 12:05:00"
    assert new[1] == "Alex" and new[2] == "2026-09-08 12:00:00" and new[6]  # sender and send time carried, logged_at set
    assert store.inbox.rows[1][4] == "A101"  # the first version is still there, untouched


def test_the_newest_row_of_a_message_is_its_state(settings):
    store = store_with(settings,
                       row(42, "A101", STATUS_PENDING),
                       row(42, "A101 300", STATUS_PENDING, edited="2026-09-08 12:05:00"),
                       row(42, "A101 300", STATUS_PROCESSED, rows_added="1", edited="2026-09-08 12:05:00"))
    latest = store.latest_row(42)
    assert latest.row == 4 and latest.status == STATUS_PROCESSED and latest.rows_added == 1
    assert store.pending_messages() == []
    assert store.update_message(42, "A101 350", at(2026, 9, 8, 14)) == STATUS_PENDING_REVISION
    assert store.inbox.appended[0][7] == 1  # rows_added carried for the hold rule


def test_skipped_flagged_or_closed_message_without_rows_is_reopened(settings):
    for status in (STATUS_SKIPPED, STATUS_NEEDS_REVIEW, STATUS_RESOLVED, STATUS_DUPLICATE):
        store = store_with(settings, row(42, "IKEA", status, rows_added="0", note="no amount"))
        assert store.update_message(42, "IKEA 900 TL", at(2026, 9, 8, 14)) == STATUS_PENDING
        assert store.inbox.appended[0][8] == f"reopened after an edit (was {status})"


def test_message_with_rows_edited_into_a_note_is_a_retraction(settings):
    store = store_with(settings, row(42, "A101 300", STATUS_PROCESSED, rows_added="1"))
    assert store.update_message(42, "[note]", at(2026, 9, 8, 14), retire_reason="private note") == STATUS_PENDING_REVISION
    assert store.inbox.appended[0][4] == "[note]" and "removed" in store.inbox.appended[0][8]


def test_pending_message_edited_into_a_note_is_closed(settings):
    store = store_with(settings, row(42, "A101 300", STATUS_PENDING))
    assert store.update_message(42, "[note]", at(2026, 9, 8, 14), retire_reason="private note") == STATUS_SKIPPED


def test_unknown_message_returns_none(settings):
    store = store_with(settings, row(1, "x", STATUS_PENDING))
    assert store.update_message(999, "y", at(2026, 9, 8, 14)) is None and store.inbox.appended == []


def test_sync_marks_and_deletions_are_new_rows_with_the_text_carried(settings):
    store = store_with(settings, row(42, "A101 300", STATUS_PENDING), row(43, "Cafe 385", STATUS_PENDING))
    pending = store.pending_messages()
    assert [m.message_id for m in pending] == [42, 43]
    store.mark_messages([(pending[0], STATUS_PROCESSED, 1, ""), (pending[1], STATUS_SKIPPED, 0, "no expense")])
    assert [(r[0], r[4], r[5], r[7], r[8]) for r in store.inbox.appended] == [("42", "A101 300", STATUS_PROCESSED, 1, ""),
                                                                            ("43", "Cafe 385", STATUS_SKIPPED, 0, "no expense")]
    assert store.pending_messages() == []
    with_rows = store.messages_with_rows()
    assert [m.message_id for m in with_rows] == [42]
    store.mark_deleted(with_rows)
    last = store.inbox.appended[-1]
    assert last[0] == "42" and last[5] == STATUS_PENDING_DELETION and last[7] == 1 and "deleted in Telegram" in last[8]
    assert store.pending_messages()[0].deletion is True and store.messages_with_rows() == []
    assert len(store.inbox.rows) == 6  # header, two originals, two marks, one deletion: nothing overwritten


def test_review_items_are_the_open_doubts_and_held_imports(settings):
    store = store_with(settings,
                       row(-7, "UBER 84", STATUS_DUPLICATE, note="looks already in the sheet (row 161)"),
                       row(50, "Cafe", STATUS_NEEDS_REVIEW, rows_added="0", note="no amount given"),
                       row(51, "x", STATUS_NEEDS_REVIEW, rows_added="0"),
                       row(51, "x", STATUS_RESOLVED, rows_added="0", note="closed"))
    items = store.review_items()
    assert [(m.message_id, m.status, m.note) for m in items] == [(-7, STATUS_DUPLICATE, "looks already in the sheet (row 161)"),
                                                                 (50, STATUS_NEEDS_REVIEW, "no amount given")]


def test_stored_texts_recognise_a_live_message_pasted_back(settings):
    store = store_with(settings, ["78", "Alex", "2026-09-19 14:02:37", "", "Migros  450 tl", STATUS_PROCESSED, "", "1", ""])
    assert store.stored_texts() == {"migros 450 tl": [at(2026, 9, 19, 14, 2).replace(second=37)]}
