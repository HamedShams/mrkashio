"""The Summary tab: a live report over the transactions tab, built by `python bot.py init-sheet`.

Everything on it is a spreadsheet formula, so it updates by itself: spend and share per category (shares among
the categorised rows; rows without a category, typically older hand-entered ones, are shown as one line for
information), spend per month, totals per currency, the ten largest expenses, plus a pie chart and a column chart. The category list
at the top of the tab is also what the category dropdown on the transactions tab offers, so adding a
category is one cell. Totals count rows in the base currency (cell B3, editable); other currencies are
listed separately rather than summed together.
"""

from __future__ import annotations

import logging

from gspread.exceptions import WorksheetNotFound

from config import Settings
from extractor import CURRENCIES, clean_categories
from sheets import SheetStore, _retry

log = logging.getLogger(__name__)

CATEGORY_SLOTS = 19  # rows 7..25 hold category names; the dropdown reads exactly this range
FIRST_CATEGORY_ROW = 7
CATEGORY_TOTAL_ROW = FIRST_CATEGORY_ROW + CATEGORY_SLOTS  # 26: total of the categorised rows, the base of the shares
UNCATEGORISED_ROW = CATEGORY_TOTAL_ROW + 1  # 27: rows without a category (older, hand-entered), shown for information
MONTH_TITLE_ROW = 29
FIRST_MONTH_ROW = 31
MONTH_SLOTS = 36
CURRENCY_TITLE_ROW = FIRST_MONTH_ROW + MONTH_SLOTS + 2  # 69
FIRST_CURRENCY_ROW = CURRENCY_TITLE_ROW + 2  # 71
TOP_TITLE_ROW = FIRST_CURRENCY_ROW + len(CURRENCIES) + 2  # 78
FIRST_TOP_ROW = TOP_TITLE_ROW + 2  # 80
TOP_COUNT = 10


def build_summary(store: SheetStore, settings: Settings, rewrite: bool = False) -> str:
    """Create the report tab (or replace it with --rewrite) and wire the category dropdown to it."""
    title = settings.summary_tab
    categories = clean_categories(store.category_options())
    existing = None
    try:
        existing = store.spreadsheet.worksheet(title)
    except WorksheetNotFound:
        pass
    if existing is not None:
        if not rewrite:
            raise FileExistsError(f"A tab called {title!r} already exists.")
        _retry(lambda: store.spreadsheet.del_worksheet(existing))
    sheet = _retry(lambda: store.spreadsheet.add_worksheet(title=title, rows=FIRST_TOP_ROW + TOP_COUNT + 4, cols=10, index=0))
    _write_cells(sheet, settings, categories)
    _apply_layout(store, sheet, settings)
    _add_charts(store, sheet)
    _point_dropdown_at(store, title)
    return (f"Built the {title!r} tab over {settings.sheet_tab!r}: spend and share by category ({len(categories)} categories), "
            f"by month, by currency, the {TOP_COUNT} largest expenses, and two charts. The category dropdown on "
            f"{settings.sheet_tab!r} now lists the categories in {title!r}!A{FIRST_CATEGORY_ROW}:A{CATEGORY_TOTAL_ROW - 1}; "
            f"edit that column to change categories. Base currency for the totals: cell B3.")


def _tab(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _write_cells(sheet, settings: Settings, categories: list[str]) -> None:
    t = _tab(settings.sheet_tab)
    c = settings.columns
    date, amount, currency, description, category = (f"{t}!${col}:${col}" for col in (c.date, c.amount, c.currency, c.description, c.category))
    base = "$B$3"
    rows: dict[str, list[list[object]]] = {}

    rows["A1"] = [["Kashio · Summary"], ["Source tab", settings.sheet_tab], ["Base currency", settings.default_currency],
                  ["Totals count rows in the base currency; other currencies are listed separately."]]
    rows["A5"] = [["By category"], ["Category", "Total", "Share", "Transactions"]]
    cat_rows = []
    for offset in range(CATEGORY_SLOTS):
        r = FIRST_CATEGORY_ROW + offset
        name = categories[offset] if offset < len(categories) else ""
        cat_rows.append([
            name,
            f'=IF($A{r}="","",SUMIFS({amount},{category},$A{r},{currency},{base}))',
            f'=IF($A{r}="","",IF($B${CATEGORY_TOTAL_ROW}=0,0,B{r}/$B${CATEGORY_TOTAL_ROW}))',
            f'=IF($A{r}="","",COUNTIFS({category},$A{r},{currency},{base}))',
        ])
    last, tot = FIRST_CATEGORY_ROW + CATEGORY_SLOTS - 1, CATEGORY_TOTAL_ROW
    cat_rows.append(["Total (categorised)", f"=SUM(B{FIRST_CATEGORY_ROW}:B{last})", f"=SUM(C{FIRST_CATEGORY_ROW}:C{last})", f"=SUM(D{FIRST_CATEGORY_ROW}:D{last})"])
    cat_rows.append(["Without a category (not in the shares)",
                     f'=SUMIFS({amount},{category},"",{currency},{base},{date},"<>")',
                     "",
                     f'=COUNTIFS({category},"",{currency},{base},{date},"<>")'])
    rows[f"A{FIRST_CATEGORY_ROW}"] = cat_rows

    rows[f"A{MONTH_TITLE_ROW}"] = [["By month"], ["Month", "Total", "Transactions"]]
    month_rows = []
    for offset in range(MONTH_SLOTS):
        r = FIRST_MONTH_ROW + offset
        first = f'=IFERROR(SORT(UNIQUE(EOMONTH(FILTER({date},ISNUMBER({date})),-1)+1)),"")' if offset == 0 else None
        month_rows.append([
            first,
            f'=IF($A{r}="","",SUMIFS({amount},{date},">="&$A{r},{date},"<"&(EOMONTH($A{r},0)+1),{currency},{base}))',
            f'=IF($A{r}="","",COUNTIFS({date},">="&$A{r},{date},"<"&(EOMONTH($A{r},0)+1),{currency},{base}))',
        ])
    rows[f"A{FIRST_MONTH_ROW}"] = month_rows

    rows[f"A{CURRENCY_TITLE_ROW}"] = [["By currency"], ["Currency", "Total", "Transactions"]]
    rows[f"A{FIRST_CURRENCY_ROW}"] = [
        [code, f"=SUMIF({currency},$A{FIRST_CURRENCY_ROW + i},{amount})", f"=COUNTIF({currency},$A{FIRST_CURRENCY_ROW + i})"]
        for i, code in enumerate(CURRENCIES)
    ]

    rows[f"A{TOP_TITLE_ROW}"] = [[f"Largest {TOP_COUNT} expenses (base currency)"], ["Date", "Description", "Amount", "Category"]]
    rows[f"A{FIRST_TOP_ROW}"] = [[
        f'=IFERROR(SORTN(FILTER({{{date},{description},{amount},{category}}},ISNUMBER({date}),ISNUMBER({amount}),{currency}={base}),{TOP_COUNT},0,3,FALSE),"")'
    ]]

    updates = []
    for anchor, block in rows.items():
        # None keeps a cell untouched (the month column spills from its first cell)
        updates.append({"range": anchor, "values": [[("" if v is None else v) for v in row] for row in block]})
    # write the month column's first cell separately so the spill area below it stays empty
    _retry(lambda: sheet.batch_update(updates, value_input_option="USER_ENTERED"))


def _apply_layout(store: SheetStore, sheet, settings: Settings) -> None:
    sid = sheet.id
    def rng(r1, r2, c1, c2):
        return {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2, "startColumnIndex": c1, "endColumnIndex": c2}
    def fmt(r1, r2, c1, c2, cell_format, fields):
        return {"repeatCell": {"range": rng(r1, r2, c1, c2), "cell": {"userEnteredFormat": cell_format}, "fields": fields}}
    bold = {"textFormat": {"bold": True}}
    requests = [
        fmt(1, 1, 0, 4, {"textFormat": {"bold": True, "fontSize": 16}}, "userEnteredFormat.textFormat"),
        fmt(3, 3, 1, 2, {"backgroundColor": {"red": 1, "green": 0.97, "blue": 0.85}}, "userEnteredFormat.backgroundColor"),
        fmt(5, 5, 0, 4, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        fmt(6, 6, 0, 4, bold, "userEnteredFormat.textFormat"),
        fmt(CATEGORY_TOTAL_ROW, CATEGORY_TOTAL_ROW, 0, 4, bold, "userEnteredFormat.textFormat"),
        fmt(UNCATEGORISED_ROW, UNCATEGORISED_ROW, 0, 4, {"textFormat": {"italic": True, "foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}}, "userEnteredFormat.textFormat"),
        fmt(FIRST_CATEGORY_ROW, UNCATEGORISED_ROW, 1, 2, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.0"}}, "userEnteredFormat.numberFormat"),
        fmt(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW, 2, 3, {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}, "userEnteredFormat.numberFormat"),
        fmt(MONTH_TITLE_ROW, MONTH_TITLE_ROW, 0, 3, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        fmt(MONTH_TITLE_ROW + 1, MONTH_TITLE_ROW + 1, 0, 3, bold, "userEnteredFormat.textFormat"),
        fmt(FIRST_MONTH_ROW, FIRST_MONTH_ROW + MONTH_SLOTS - 1, 0, 1, {"numberFormat": {"type": "DATE", "pattern": "mmm yyyy"}}, "userEnteredFormat.numberFormat"),
        fmt(FIRST_MONTH_ROW, FIRST_MONTH_ROW + MONTH_SLOTS - 1, 1, 2, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.0"}}, "userEnteredFormat.numberFormat"),
        fmt(CURRENCY_TITLE_ROW, CURRENCY_TITLE_ROW, 0, 3, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        fmt(CURRENCY_TITLE_ROW + 1, CURRENCY_TITLE_ROW + 1, 0, 3, bold, "userEnteredFormat.textFormat"),
        fmt(FIRST_CURRENCY_ROW, FIRST_CURRENCY_ROW + len(CURRENCIES) - 1, 1, 2, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.0"}}, "userEnteredFormat.numberFormat"),
        fmt(TOP_TITLE_ROW, TOP_TITLE_ROW, 0, 4, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        fmt(TOP_TITLE_ROW + 1, TOP_TITLE_ROW + 1, 0, 4, bold, "userEnteredFormat.textFormat"),
        fmt(FIRST_TOP_ROW, FIRST_TOP_ROW + TOP_COUNT - 1, 0, 1, {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}, "userEnteredFormat.numberFormat"),
        fmt(FIRST_TOP_ROW, FIRST_TOP_ROW + TOP_COUNT - 1, 2, 3, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.0"}}, "userEnteredFormat.numberFormat"),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 230}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 4}, "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True}}, "fields": "gridProperties.hideGridlines"}},
    ]
    _retry(lambda: store.spreadsheet.batch_update({"requests": requests}))


def _add_charts(store: SheetStore, sheet) -> None:
    sid = sheet.id
    def src(r1, r2, col):
        return {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2, "startColumnIndex": col, "endColumnIndex": col + 1}
    def position(row, col, width, height):
        return {"overlayPosition": {"anchorCell": {"sheetId": sid, "rowIndex": row - 1, "columnIndex": col}, "widthPixels": width, "heightPixels": height}}
    pie = {"addChart": {"chart": {
        "spec": {"title": "Share by category", "pieChart": {
            "legendPosition": "RIGHT_LEGEND",
            "domain": {"sourceRange": {"sources": [src(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW - 1, 0)]}},
            "series": {"sourceRange": {"sources": [src(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW - 1, 1)]}},
        }},
        "position": position(5, 5, 520, 340),
    }}}
    columns = {"addChart": {"chart": {
        "spec": {"title": "Spend by month", "basicChart": {
            "chartType": "COLUMN", "legendPosition": "NO_LEGEND",
            "axis": [{"position": "BOTTOM_AXIS", "title": "Month"}, {"position": "LEFT_AXIS", "title": "Total"}],
            "domains": [{"domain": {"sourceRange": {"sources": [src(FIRST_MONTH_ROW, FIRST_MONTH_ROW + MONTH_SLOTS - 1, 0)]}}}],
            "series": [{"series": {"sourceRange": {"sources": [src(FIRST_MONTH_ROW, FIRST_MONTH_ROW + MONTH_SLOTS - 1, 1)]}}, "targetAxis": "LEFT_AXIS"}],
        }},
        "position": position(MONTH_TITLE_ROW, 5, 520, 340),
    }}}
    _retry(lambda: store.spreadsheet.batch_update({"requests": [pie, columns]}))


def _point_dropdown_at(store: SheetStore, summary_title: str) -> None:
    """The category column's dropdown offers exactly the names in the Summary's category list."""
    c = store.columns
    col = c.index(c.category)
    request = {"setDataValidation": {
        "range": {"sheetId": store.target.id, "startRowIndex": 1, "endRowIndex": store.target.row_count, "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"={_tab(summary_title)}!$A${FIRST_CATEGORY_ROW}:$A${CATEGORY_TOTAL_ROW - 1}"}]},
                 "showCustomUi": True, "strict": False},
    }}
    _retry(lambda: store.spreadsheet.batch_update({"requests": [request]}))
