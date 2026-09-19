from datetime import date
from types import SimpleNamespace

import pytest

from sheets import SheetStore, _as_date


def test_cell_values_become_dates_or_nothing():
    assert _as_date(46232) == date(2026, 7, 29)
    assert _as_date("29/07/2026") == date(2026, 7, 29) and _as_date("2026-07-29") == date(2026, 7, 29)
    assert _as_date("Change or add categories") is None and _as_date("") is None and _as_date(True) is None


def store_with(target_values, settings):
    store = SheetStore.__new__(SheetStore)
    store.settings = settings
    store.columns = settings.columns
    store.target = SimpleNamespace(get_values=lambda *args, **kwargs: target_values)
    return store


def test_last_used_row_ignores_prefilled_currency_column(settings):
    values = [["Date", "Amount", "Currency", "Description"], ["2026-07-29", "290", "TRY", "A101"], ["", "", "TRY", ""], ["", "", "TRY", ""]]
    assert store_with(values, settings).last_used_row() == 2


def test_guardrail_refuses_to_write_over_existing_data(settings):
    store = store_with([["", "", "TRY", "", "", ""]], settings)
    store._assert_empty(153, 153)  # only the default currency: fine
    store = store_with([["2026-09-01", "12", "TRY", "manual row", "", ""]], settings)
    with pytest.raises(RuntimeError, match="refusing to write: row 153"):
        store._assert_empty(153, 153)
