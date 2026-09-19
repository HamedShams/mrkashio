"""replace_transactions: update in place, delete extras bottom-up, append the rest, touch only noted rows."""

from datetime import date
from types import SimpleNamespace

import pytest

from sheets import NOTE_PREFIX, SheetStore, TransactionRow


class StubTarget:
    def __init__(self, notes):
        self.notes, self.id, self.row_count = dict(notes), 7, 500
        self.batches, self.deleted, self.inserted_notes = [], [], []

    def get_notes(self, grid_range=None):
        if ":" in grid_range:  # whole column
            top = max(self.notes) if self.notes else 1
            return [[self.notes.get(r, "")] for r in range(1, top + 1)]
        row = int(grid_range[1:])
        return [[self.notes.get(row, "")]]

    def batch_update(self, updates, value_input_option=None):
        self.batches.append(updates)

    def delete_rows(self, start, end=None):
        self.deleted.append(start)
        self.notes = {(r if r < start else r - 1): n for r, n in self.notes.items() if r != start}

    def insert_notes(self, notes):
        self.inserted_notes.append(notes)

    def get_values(self, *a, **k):
        return [[""] * 6]

    def add_rows(self, n):
        self.row_count += n


def store_with(notes, settings):
    store = SheetStore.__new__(SheetStore)
    store.settings, store.columns = settings, settings.columns
    store.target = StubTarget(notes)
    store.spreadsheet = SimpleNamespace(batch_update=lambda body: None)
    store.last_used_row = lambda: max(store.target.notes) if store.target.notes else 1
    return store


def row(desc, amount=10.0, mid=42):
    return TransactionRow(date(2026, 9, 8), amount, "TRY", desc, "Other", mid)


def test_same_count_updates_in_place(settings):
    store = store_with({160: f"{NOTE_PREFIX}42", 161: f"{NOTE_PREFIX}42", 162: f"{NOTE_PREFIX}43"}, settings)
    result = store.replace_transactions(42, [row("a"), row("b")])
    assert (result.updated, result.deleted, result.appended, result.rows) == (2, 0, 0, [160, 161])
    assert store.target.deleted == [] and [b[0]["range"] for b in store.target.batches] == ["B160:B160", "B161:B161"]


def test_fewer_rows_deletes_the_extra_ones_bottom_up(settings):
    store = store_with({160: f"{NOTE_PREFIX}42", 161: f"{NOTE_PREFIX}42", 162: f"{NOTE_PREFIX}42"}, settings)
    result = store.replace_transactions(42, [row("only")])
    assert (result.updated, result.deleted, result.appended) == (1, 2, 0)
    assert store.target.deleted == [162, 161]


def test_retraction_deletes_everything_and_touches_nothing_else(settings):
    store = store_with({160: f"{NOTE_PREFIX}42", 161: f"{NOTE_PREFIX}43"}, settings)
    result = store.replace_transactions(42, [])
    assert (result.updated, result.deleted) == (0, 1) and store.target.deleted == [160]
    assert store.target.notes == {160: f"{NOTE_PREFIX}43"}  # the other message's row moved up, untouched


def test_more_rows_appends_the_extra_ones(settings):
    store = store_with({160: f"{NOTE_PREFIX}42"}, settings)
    result = store.replace_transactions(42, [row("a"), row("b")])
    assert (result.updated, result.appended) == (1, 1) and result.rows == [160, 161]


def test_deletion_refuses_when_the_note_is_gone(settings):
    store = store_with({160: f"{NOTE_PREFIX}42", 161: f"{NOTE_PREFIX}42"}, settings)
    original = store.target.get_notes
    store.target.get_notes = lambda grid_range=None: [[""]] if grid_range == "B161" else original(grid_range)
    with pytest.raises(RuntimeError, match="refusing to delete row 161"):
        store.replace_transactions(42, [row("a")])
