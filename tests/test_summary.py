"""The Summary builder writes formulas over the configured tab and columns, and points the dropdown at its list."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

import summary
from summary import CATEGORY_TOTAL_ROW, FIRST_CATEGORY_ROW, UNCATEGORISED_ROW, _point_dropdown_at, _write_cells, build_summary


class StubSheet:
    id = 5

    def __init__(self):
        self.batches = []

    def batch_update(self, updates, value_input_option=None):
        self.batches.append(updates)


def cells(updates):
    return {u["range"]: u["values"] for u in updates}


def test_formulas_use_the_configured_tab_and_columns(settings):
    sheet = StubSheet()
    _write_cells(sheet, replace(settings, sheet_tab="My Spend"), ["Groceries", "Other"])
    written = cells(sheet.batches[0])
    category_block = written[f"A{FIRST_CATEGORY_ROW}"]
    assert category_block[0][0] == "Groceries" and category_block[1][0] == "Other" and category_block[2][0] == ""
    assert category_block[0][1] == "=IF($A7=\"\",\"\",SUMIFS('My Spend'!$C:$C,'My Spend'!$G:$G,$A7,'My Spend'!$D:$D,$B$3))"
    assert category_block[CATEGORY_TOTAL_ROW - FIRST_CATEGORY_ROW][0] == "Total (categorised)"
    assert category_block[UNCATEGORISED_ROW - FIRST_CATEGORY_ROW][0].startswith("Without a category")
    assert category_block[0][2] == "=IF($A7=\"\",\"\",IF($B$26=0,0,B7/$B$26))"  # shares over the categorised total
    assert written["A31"][0][0].startswith("=IFERROR(SORT(UNIQUE(EOMONTH(FILTER('My Spend'!$B:$B")
    assert written["A31"][1][0] == ""  # the month column spills from its first cell
    assert written["A1"][2] == ["Base currency", "TRY"]


def test_columns_follow_the_layout_settings(settings):
    sheet = StubSheet()
    columns = replace(settings.columns, amount="F", category="H")
    _write_cells(sheet, replace(settings, columns=columns), ["Groceries"])
    formula = cells(sheet.batches[0])[f"A{FIRST_CATEGORY_ROW}"][0][1]
    assert "!$F:$F" in formula and "!$H:$H" in formula


def test_dropdown_points_at_the_category_list(settings):
    requests = []
    store = SimpleNamespace(columns=settings.columns, target=SimpleNamespace(id=9, row_count=500),
                            spreadsheet=SimpleNamespace(batch_update=lambda body: requests.append(body)))
    _point_dropdown_at(store, "Summary")
    rule = requests[0]["requests"][0]["setDataValidation"]
    assert rule["range"]["startColumnIndex"] == 6 and rule["rule"]["condition"]["values"][0]["userEnteredValue"] == "='Summary'!$A$7:$A$25"


def test_existing_tab_is_kept_unless_rewrite(settings, monkeypatch):
    store = SimpleNamespace(category_options=lambda: ["Groceries"], spreadsheet=SimpleNamespace(worksheet=lambda title: object()))
    with pytest.raises(FileExistsError):
        build_summary(store, settings, rewrite=False)
