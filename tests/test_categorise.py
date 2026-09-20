"""categorise: rows with a description and no category, one Claude call, and a write that touches only empty category cells."""

from types import SimpleNamespace

import pytest

import categorise
from categorise import Candidate, answer_model, apply, candidates, plan
from tests.test_extractor import FakeClient


class StubTarget:
    def __init__(self, rows, categories=None):
        self.rows, self.categories, self.batches = rows, dict(categories or {}), []

    def get_values(self, rng, value_render_option=None):
        if rng.startswith("G"):  # the re-check before writing: G{first}:G{last}
            first, last = (int(part[1:]) for part in rng.split(":"))
            return [[self.categories.get(row, "")] for row in range(first, last + 1)]
        return self.rows

    def batch_update(self, updates, value_input_option=None):
        self.batches.append(updates)


def store_with(settings, rows, categories=None):
    return SimpleNamespace(columns=settings.columns, target=StubTarget(rows, categories))


ROWS = [
    [46188, 58029000, "TOMAN", "SnappTrip - Flight Ticket to Istanbul", "Shiva", ""],  # row 5
    [46194, "CHANGED €100 Euro to 5,000 TL", "", "", "", ""],  # row 6: a note typed across the row
    [46198, "₺674", "TRY", "Groceries - A101", "", ""],  # row 7: amount typed as text, still a purchase
    [46200, 300, "TRY", "Lunch (Wrap)", "", "Eating Out"],  # row 8: already categorised
    [],  # row 9: blank
]


def test_candidates_are_rows_with_a_description_and_no_category(settings):
    found = candidates(store_with(settings, ROWS), 5, 9)
    assert [(c.row, c.date, c.amount, c.currency, c.description) for c in found] == [
        (5, "15/06/2026", 58029000, "TOMAN", "SnappTrip - Flight Ticket to Istanbul"), (7, "25/06/2026", "₺674", "TRY", "Groceries - A101")]


def test_plan_asks_once_retries_the_missing_rows_and_limits_categories(settings):
    rows = [Candidate(5, "18/06/2026", 58029000, "TOMAN", "SnappTrip - Flight Ticket to Istanbul"), Candidate(7, "28/06/2026", "₺674", "TRY", "Groceries - A101")]
    client = FakeClient(['{"rows": [{"row": 5, "category": "Leisure & Travel"}, {"row": 99, "category": "Other"}]}',
                        '```json\n{"rows": [{"row": 7, "category": "Groceries"}]}\n```'])
    result = plan(client, settings, rows, ["Groceries", "Leisure & Travel", "Other"])
    assert result.mapping == {5: "Leisure & Travel", 7: "Groceries"} and result.unanswered == [] and result.calls == 2
    assert client.requests[0]["output_config"] == {"effort": "high"} and "row 5 · 18/06/2026 · 58029000 TOMAN · SnappTrip" in client.requests[0]["messages"][0]["content"]
    assert client.requests[1]["messages"][0]["content"].startswith("row 7 ·")  # only the missing row is asked again
    assert "G5: Leisure & Travel" in result.describe() and "Per category: Leisure & Travel 1, Groceries 1" in result.describe()
    with pytest.raises(Exception):
        answer_model(["Groceries"]).model_validate({"rows": [{"row": 5, "category": "Eating Out"}]})


def test_a_wrong_answer_is_reported_and_the_row_stays_unanswered(settings):
    rows = [Candidate(5, "18/06/2026", 1, "TRY", "x")]
    result = plan(FakeClient(["no json here", '{"rows": [{"row": 5, "category": "Nope"}]}']), settings, rows, ["Groceries"])
    assert result.mapping == {} and result.unanswered == [5] and len(result.problems) == 2


def test_apply_writes_only_the_category_cells_and_refuses_a_filled_one(settings):
    store = store_with(settings, ROWS)
    assert apply(store, {5: "Leisure & Travel", 7: "Groceries"}) == "G5:G7"
    assert store.target.batches == [[{"range": "G5", "values": [["Leisure & Travel"]]}, {"range": "G7", "values": [["Groceries"]]}]]
    store = store_with(settings, ROWS, categories={7: "Eating Out"})
    with pytest.raises(RuntimeError, match="G7 already holds 'Eating Out'"):
        apply(store, {5: "Leisure & Travel", 7: "Groceries"})
    assert store.target.batches == []  # nothing at all was written
    assert apply(store, {}) == ""
