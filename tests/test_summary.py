"""The Summary builder: formulas over the configured tab and columns, conversion through the rates table, the dropdowns."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

import summary
from summary import (
    CATEGORY_TOTAL_ROW,
    FIRST_CATEGORY_ROW,
    FIRST_RATE_ROW,
    RATES_TITLE,
    UNCATEGORISED_ROW,
    _point_dropdown_at,
    _write_cells,
    build_summary,
    default_rates,
    read_rates,
)


class StubSheet:
    id = 5

    def __init__(self):
        self.batches = []

    def batch_update(self, updates, value_input_option=None):
        self.batches.append(updates)


def cells(updates):
    return {u["range"]: u["values"] for u in updates}


def test_every_figure_is_converted_into_the_base_currency_through_the_rates_table(settings):
    sheet = StubSheet()
    _write_cells(sheet, replace(settings, sheet_tab="My Spend"), ["Groceries", "Other"])
    written = cells(sheet.batches[0])
    block = written[f"A{FIRST_CATEGORY_ROW}"]
    assert block[0][0] == "Groceries" and block[1][0] == "Other" and block[2][0] == ""
    total = block[0][1]
    assert total.startswith('=IF($A7="","",IFERROR(ARRAYFORMULA(SUMPRODUCT((\'My Spend\'!$G:$G=$A7)')
    assert "IF(ISNUMBER('My Spend'!$C:$C),'My Spend'!$C:$C,0)" in total  # notes typed across a row count as 0
    assert "VLOOKUP(IF('My Spend'!$D:$D=\"\",\"TRY\",'My Spend'!$D:$D),$A$80:$B$84,2,FALSE)" in total  # a blank currency is the default
    assert total.endswith('/VLOOKUP($B$3,$A$80:$B$84,2,FALSE))),""))')  # divided by the base currency's rate
    assert block[CATEGORY_TOTAL_ROW - FIRST_CATEGORY_ROW][0] == "Total (categorised)"
    assert block[UNCATEGORISED_ROW - FIRST_CATEGORY_ROW][0].startswith("Without a category")
    assert block[0][2] == "=IF($A7=\"\",\"\",IF($B$26=0,0,B7/$B$26))"  # shares over the categorised total
    assert written["A31"][0][0].startswith("=IFERROR(SORT(UNIQUE(EOMONTH(FILTER('My Spend'!$B:$B")
    assert "SUMPRODUCT(('My Spend'!$B:$B>=$A31)*('My Spend'!$B:$B<EOMONTH($A31,0)+1)" in written["A31"][0][1]
    assert written["A31"][1][0] == ""  # the month column spills from its first cell
    assert written["A1"][2] == ["Base currency", "TRY"]
    assert written["A5"][1][1] == '="In "&$B$3' and written["A69"][1] == ["Currency", "As logged", "Transactions", '="In "&$B$3']
    assert written["A71"][0][3].startswith("=IFERROR(B71*VLOOKUP($A71,$A$80:$B$84,2,FALSE)/VLOOKUP($B$3")
    top = written["A89"][0][0]
    assert top.startswith("=IFERROR(ARRAYFORMULA(SORTN(FILTER({'My Spend'!$B:$B,'My Spend'!$E:$E,") and ",10,0,3,FALSE)" in top


def test_default_rates_are_one_for_the_base_google_finance_for_the_rest_and_blank_for_toman(settings):
    assert default_rates(settings) == {"TRY": 1, "TOMAN": "", "EUR": '=GOOGLEFINANCE("CURRENCY:EURTRY")',
                                       "USD": '=GOOGLEFINANCE("CURRENCY:USDTRY")', "GBP": '=GOOGLEFINANCE("CURRENCY:GBPTRY")'}
    assert default_rates(replace(settings, default_currency="USD"))["USD"] == 1
    sheet = StubSheet()
    _write_cells(sheet, settings, ["Groceries"], rates={"TOMAN": "=1/178000", "USD": "='Transactions'!$R$5", "XXX": 9})
    block = cells(sheet.batches[0])[f"A{FIRST_RATE_ROW}"]
    assert [row[:2] for row in block] == [["TRY", 1], ["TOMAN", "=1/178000"], ["EUR", '=GOOGLEFINANCE("CURRENCY:EURTRY")'],
                                          ["USD", "='Transactions'!$R$5"], ["GBP", '=GOOGLEFINANCE("CURRENCY:GBPTRY")']]
    assert block[1][2].startswith("enter by hand")


def test_rates_of_a_previous_tab_are_read_back_for_a_rewrite():
    old = SimpleNamespace(get_values=lambda rng, value_render_option=None: [
        ["Kashio · Summary"], ["Source tab", "T"], [], ["By currency"], ["TRY", "=SUMIF(...)"],  # a currency row outside the section
        [RATES_TITLE], ["Currency", "1 unit is worth"], ["TRY", 1], ["TOMAN", "=1/178000"], ["EUR", ""], ["USD", "='T'!$R$5"], ["GBP", 0.031],
        [], ["Largest 10 expenses"], ["EUR", "=nope"],
    ])
    assert read_rates(old) == {"TRY": 1, "TOMAN": "=1/178000", "USD": "='T'!$R$5", "GBP": 0.031}


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


def test_base_currency_cell_is_a_dropdown_of_the_supported_currencies(settings):
    requests = []
    store = SimpleNamespace(spreadsheet=SimpleNamespace(batch_update=lambda body: requests.append(body)))
    summary._apply_layout(store, StubSheet(), settings)
    validations = [r["setDataValidation"] for r in requests[0]["requests"] if "setDataValidation" in r]
    assert len(validations) == 1
    rule, cell = validations[0]["rule"], validations[0]["range"]
    assert (cell["startRowIndex"], cell["endRowIndex"], cell["startColumnIndex"], cell["endColumnIndex"]) == (2, 3, 1, 2)  # B3
    assert [v["userEnteredValue"] for v in rule["condition"]["values"]] == ["TRY", "TOMAN", "EUR", "USD", "GBP"] and rule["strict"] is True


def test_existing_tab_is_kept_unless_rewrite(settings, monkeypatch):
    store = SimpleNamespace(category_options=lambda: ["Groceries"], spreadsheet=SimpleNamespace(worksheet=lambda title: object()))
    with pytest.raises(FileExistsError):
        build_summary(store, settings, rewrite=False)
