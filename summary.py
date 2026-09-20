"""The Summary tab: a live report over the transactions tab, built by `python bot.py init-sheet`.

Everything on it is a spreadsheet formula, so it updates by itself. Every amount is converted into the base
currency chosen in the dropdown in B3, using the exchange-rate table on the same tab (one editable cell per
currency: the value of one unit, all measured in the same currency of your choice, since only the ratios
matter). Switching B3 switches the whole report: spend and share per category (shares among the categorised
rows; rows without a category, typically older hand-entered ones, are shown as one line for information),
spend per month, totals per currency as logged and converted, the ten largest expenses, a donut chart and a
column chart. The category list at the top of the tab is also what the category dropdown on the
transactions tab offers, so adding a category is one cell. Rows whose amount is not a number (a note typed
across a row, a blank line) are ignored by every formula.
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
RATES_TITLE_ROW = FIRST_CURRENCY_ROW + len(CURRENCIES) + 2  # 78
FIRST_RATE_ROW = RATES_TITLE_ROW + 2  # 80
LAST_RATE_ROW = FIRST_RATE_ROW + len(CURRENCIES) - 1  # 84
TOP_TITLE_ROW = LAST_RATE_ROW + 3  # 87
FIRST_TOP_ROW = TOP_TITLE_ROW + 2  # 89
TOP_COUNT = 10
RATES_TITLE = "Exchange rates"
RATES_RANGE = f"$A${FIRST_RATE_ROW}:$B${LAST_RATE_ROW}"
NO_FEED = ("TOMAN",)  # currencies without a usable Google Finance feed: the rate is entered by hand


def build_summary(store: SheetStore, settings: Settings, rewrite: bool = False) -> str:
    """Create the report tab (or replace it with --rewrite, keeping its exchange rates) and wire the category dropdown to it."""
    title = settings.summary_tab
    categories = clean_categories(store.category_options())
    existing = None
    rates: dict[str, object] = {}
    try:
        existing = store.spreadsheet.worksheet(title)
    except WorksheetNotFound:
        pass
    if existing is not None:
        if not rewrite:
            raise FileExistsError(f"A tab called {title!r} already exists.")
        rates = read_rates(existing)
        _retry(lambda: store.spreadsheet.del_worksheet(existing))
    sheet = _retry(lambda: store.spreadsheet.add_worksheet(title=title, rows=FIRST_TOP_ROW + TOP_COUNT + 4, cols=12, index=0))
    _write_cells(sheet, settings, categories, rates)
    _apply_layout(store, sheet, settings)
    _add_charts(store, sheet)
    _point_dropdown_at(store, title)
    kept = f" Kept the exchange rates of the previous tab ({', '.join(sorted(rates))})." if rates else ""
    return (f"Built the {title!r} tab over {settings.sheet_tab!r}: spend and share by category ({len(categories)} categories), "
            f"by month, by currency, the {TOP_COUNT} largest expenses, and two charts, everything converted into the currency "
            f"chosen in B3 with the exchange rates in A{FIRST_RATE_ROW}:B{LAST_RATE_ROW}.{kept} The category dropdown on "
            f"{settings.sheet_tab!r} now lists the categories in {title!r}!A{FIRST_CATEGORY_ROW}:A{CATEGORY_TOTAL_ROW - 1}; "
            f"edit that column to change categories.")


def read_rates(sheet) -> dict[str, object]:
    """The exchange rates a previous version of the tab holds (formulas or numbers), by currency code, so --rewrite keeps them."""
    found: dict[str, object] = {}
    inside = False
    for row in _retry(lambda: sheet.get_values("A1:B200", value_render_option="FORMULA")):
        first = str(row[0]).strip() if row else ""
        if first == RATES_TITLE:
            inside = True
            continue
        if not inside:
            continue
        if first.upper() in CURRENCIES:
            value = row[1] if len(row) > 1 else ""
            if str(value).strip() != "":
                found[first.upper()] = value
        elif not first and found:
            break
    return found


def default_rates(settings: Settings) -> dict[str, object]:
    """Value of one unit of each currency in the default currency: 1 for the default, Google Finance for the rest, blank without a feed."""
    base = settings.default_currency
    rates: dict[str, object] = {}
    for code in CURRENCIES:
        if code == base:
            rates[code] = 1
        elif code in NO_FEED or base in NO_FEED:
            rates[code] = ""
        else:
            rates[code] = f'=GOOGLEFINANCE("CURRENCY:{code}{base}")'
    return rates


def _tab(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _write_cells(sheet, settings: Settings, categories: list[str], rates: dict[str, object] | None = None) -> None:
    t = _tab(settings.sheet_tab)
    c = settings.columns
    date, amount, currency, description, category = (f"{t}!${col}:${col}" for col in (c.date, c.amount, c.currency, c.description, c.category))
    base = "$B$3"
    # Per row: the amount as a number (0 for notes and blanks) times the value of one unit of its currency (0 when the rate is
    # missing), divided by the value of one unit of the base currency. Everything array-wise over the transactions tab.
    rate = f'IFERROR(1*VLOOKUP(IF({currency}="","{settings.default_currency}",{currency}),{RATES_RANGE},2,FALSE),0)'
    base_rate = f"VLOOKUP({base},{RATES_RANGE},2,FALSE)"
    number = f"IF(ISNUMBER({amount}),{amount},0)"
    converted = f"{number}*{rate}/{base_rate}"

    def total(condition: str) -> str:
        return f'IFERROR(ARRAYFORMULA(SUMPRODUCT({condition}*{converted})),"")'

    def count(condition: str) -> str:
        return f"ARRAYFORMULA(SUMPRODUCT({condition}*ISNUMBER({amount})))"

    in_base = f'="In "&{base}'
    rows: dict[str, list[list[object]]] = {}
    rows["A1"] = [["Kashio · Summary"], ["Source tab", settings.sheet_tab], ["Base currency", settings.default_currency],
                  ["Pick the currency in B3: every amount on this tab is converted into it with the exchange rates below, "
                   "so the whole report switches with that cell."]]
    rows["A5"] = [["By category"], ["Category", in_base, "Share", "Transactions"]]
    cat_rows: list[list[object]] = []
    for offset in range(CATEGORY_SLOTS):
        r = FIRST_CATEGORY_ROW + offset
        name = categories[offset] if offset < len(categories) else ""
        cat_rows.append([
            name,
            f'=IF($A{r}="","",{total(f"({category}=$A{r})")})',
            f'=IF($A{r}="","",IF($B${CATEGORY_TOTAL_ROW}=0,0,B{r}/$B${CATEGORY_TOTAL_ROW}))',
            f'=IF($A{r}="","",{count(f"({category}=$A{r})")})',
        ])
    last = FIRST_CATEGORY_ROW + CATEGORY_SLOTS - 1
    cat_rows.append(["Total (categorised)", f"=SUM(B{FIRST_CATEGORY_ROW}:B{last})", f"=SUM(C{FIRST_CATEGORY_ROW}:C{last})", f"=SUM(D{FIRST_CATEGORY_ROW}:D{last})"])
    uncategorised = f'({category}="")'
    cat_rows.append(["Without a category (not in the shares)", f"={total(uncategorised)}", "", f"={count(uncategorised)}"])
    rows[f"A{FIRST_CATEGORY_ROW}"] = cat_rows

    rows[f"A{MONTH_TITLE_ROW}"] = [["By month"], ["Month", in_base, "Transactions"]]
    month_rows: list[list[object]] = []
    for offset in range(MONTH_SLOTS):
        r = FIRST_MONTH_ROW + offset
        first = f'=IFERROR(SORT(UNIQUE(EOMONTH(FILTER({date},ISNUMBER({date})),-1)+1)),"")' if offset == 0 else None
        in_month = f"({date}>=$A{r})*({date}<EOMONTH($A{r},0)+1)"
        month_rows.append([first, f'=IF($A{r}="","",{total(in_month)})', f'=IF($A{r}="","",{count(in_month)})'])
    rows[f"A{FIRST_MONTH_ROW}"] = month_rows

    rows[f"A{CURRENCY_TITLE_ROW}"] = [["By currency"], ["Currency", "As logged", "Transactions", in_base]]
    rows[f"A{FIRST_CURRENCY_ROW}"] = [
        [code, f"=SUMIF({currency},$A{FIRST_CURRENCY_ROW + i},{amount})", f"=COUNTIF({currency},$A{FIRST_CURRENCY_ROW + i})",
         f'=IFERROR(B{FIRST_CURRENCY_ROW + i}*VLOOKUP($A{FIRST_CURRENCY_ROW + i},{RATES_RANGE},2,FALSE)/{base_rate},"rate missing")']
        for i, code in enumerate(CURRENCIES)
    ]

    values = default_rates(settings) | {code: value for code, value in (rates or {}).items() if code in CURRENCIES}
    rows[f"A{RATES_TITLE_ROW}"] = [[RATES_TITLE], ["Currency", "1 unit is worth", "How to fill this in"]]
    rows[f"A{FIRST_RATE_ROW}"] = [
        [code, values[code],
         "1 by definition" if code == settings.default_currency else
         "enter by hand: no reliable market feed" if code in NO_FEED else
         f"Google Finance, in {settings.default_currency}; replace with your own rate or a reference to your own table"]
        for code in CURRENCIES
    ]

    rows[f"A{TOP_TITLE_ROW}"] = [[f"Largest {TOP_COUNT} expenses"], ["Date", "Description", in_base, "Category", "As logged"]]
    rows[f"A{FIRST_TOP_ROW}"] = [[
        f'=IFERROR(ARRAYFORMULA(SORTN(FILTER({{{date},{description},{converted},{category},{amount}&" "&{currency}}},'
        f'ISNUMBER({date}),ISNUMBER({amount})),{TOP_COUNT},0,3,FALSE)),"")'
    ]]

    updates = []
    for anchor, block in rows.items():
        # None keeps a cell untouched (the month column spills from its first cell)
        updates.append({"range": anchor, "values": [[("" if v is None else v) for v in row] for row in block]})
    _retry(lambda: sheet.batch_update(updates, value_input_option="USER_ENTERED"))


GREEN = {"red": 0.118, "green": 0.420, "blue": 0.333}  # header bands
GREEN_SOFT = {"red": 0.890, "green": 0.941, "blue": 0.918}  # column headers, totals
BAND = {"red": 0.965, "green": 0.976, "blue": 0.969}  # every second row
WHITE = {"red": 1, "green": 1, "blue": 1}
INK = {"red": 0.102, "green": 0.149, "blue": 0.125}
GREY = {"red": 0.42, "green": 0.42, "blue": 0.42}
ACCENT = {"red": 0.710, "green": 0.220, "blue": 0.184}  # editable cells: the base currency and the rates
EDITABLE = {"red": 1, "green": 0.96, "blue": 0.88}
CHART_WIDTH, CHART_HEIGHT = 560, 360
TITLE_WIDTH = 5  # columns A..E


def _apply_layout(store: SheetStore, sheet, settings: Settings) -> None:
    """Colour bands for titles, tinted column headers, alternating rows, borders, widths. Cosmetic only."""
    sid = sheet.id
    last_month = FIRST_MONTH_ROW + MONTH_SLOTS - 1
    last_currency = FIRST_CURRENCY_ROW + len(CURRENCIES) - 1
    last_top = FIRST_TOP_ROW + TOP_COUNT - 1

    def rng(r1, r2, c1, c2):
        return {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2, "startColumnIndex": c1, "endColumnIndex": c2}

    def fmt(r1, r2, c1, c2, cell_format, fields):
        return {"repeatCell": {"range": rng(r1, r2, c1, c2), "cell": {"userEnteredFormat": cell_format}, "fields": fields}}

    def number(r1, r2, col, pattern, kind="NUMBER"):
        return fmt(r1, r2, col, col + 1, {"numberFormat": {"type": kind, "pattern": pattern}, "horizontalAlignment": "RIGHT"},
                   "userEnteredFormat(numberFormat,horizontalAlignment)")

    def editable(r1, r2, col):
        return [
            fmt(r1, r2, col, col + 1, {"backgroundColor": EDITABLE, "textFormat": {"bold": True, "foregroundColor": ACCENT}},
                "userEnteredFormat(backgroundColor,textFormat)"),
            {"updateBorders": {"range": rng(r1, r2, col, col + 1), "top": {"style": "SOLID", "color": ACCENT}, "bottom": {"style": "SOLID", "color": ACCENT},
                               "left": {"style": "SOLID", "color": ACCENT}, "right": {"style": "SOLID", "color": ACCENT}}},
        ]

    def section_title(row, width=4):
        return [
            {"mergeCells": {"range": rng(row, row, 0, width), "mergeType": "MERGE_ALL"}},
            fmt(row, row, 0, width, {"backgroundColor": GREEN, "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": WHITE},
                                     "verticalAlignment": "MIDDLE", "padding": {"left": 8}},
                "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,padding)"),
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": row - 1, "endIndex": row},
                                           "properties": {"pixelSize": 30}, "fields": "pixelSize"}},
        ]

    def column_header(row, width=4):
        return [
            fmt(row, row, 0, width, {"backgroundColor": GREEN_SOFT, "textFormat": {"bold": True, "foregroundColor": INK}},
                "userEnteredFormat(backgroundColor,textFormat)"),
            {"updateBorders": {"range": rng(row, row, 0, width), "bottom": {"style": "SOLID", "width": 1, "color": GREEN}}},
        ]

    def banding(r1, r2, width=4):
        return {"addBanding": {"bandedRange": {"range": rng(r1, r2, 0, width),
                                               "rowProperties": {"firstBandColor": WHITE, "secondBandColor": BAND}}}}

    requests = [
        # title band
        {"mergeCells": {"range": rng(1, 1, 0, TITLE_WIDTH), "mergeType": "MERGE_ALL"}},
        fmt(1, 1, 0, TITLE_WIDTH, {"backgroundColor": GREEN, "textFormat": {"bold": True, "fontSize": 18, "foregroundColor": WHITE},
                                   "verticalAlignment": "MIDDLE", "padding": {"left": 8}},
            "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,padding)"),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 48}, "fields": "pixelSize"}},
        fmt(2, 3, 0, 1, {"textFormat": {"bold": True, "foregroundColor": GREY}}, "userEnteredFormat.textFormat"),
        *editable(3, 3, 1),
        fmt(3, 3, 1, 2, {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"),
        # B3 is a dropdown of the supported currencies: choosing one switches every figure and chart on the tab
        {"setDataValidation": {"range": rng(3, 3, 1, 2),
                               "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": code} for code in CURRENCIES]},
                                        "showCustomUi": True, "strict": True}}},
        {"mergeCells": {"range": rng(4, 4, 0, TITLE_WIDTH), "mergeType": "MERGE_ALL"}},
        fmt(4, 4, 0, TITLE_WIDTH, {"textFormat": {"italic": True, "foregroundColor": GREY, "fontSize": 9}, "wrapStrategy": "WRAP"},
            "userEnteredFormat(textFormat,wrapStrategy)"),
        # by category
        *section_title(5), *column_header(6), banding(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW - 1),
        fmt(CATEGORY_TOTAL_ROW, CATEGORY_TOTAL_ROW, 0, 4, {"backgroundColor": GREEN_SOFT, "textFormat": {"bold": True}},
            "userEnteredFormat(backgroundColor,textFormat)"),
        {"updateBorders": {"range": rng(CATEGORY_TOTAL_ROW, CATEGORY_TOTAL_ROW, 0, 4), "top": {"style": "SOLID", "width": 1, "color": GREEN}}},
        fmt(UNCATEGORISED_ROW, UNCATEGORISED_ROW, 0, 4, {"textFormat": {"italic": True, "foregroundColor": GREY}}, "userEnteredFormat.textFormat"),
        number(FIRST_CATEGORY_ROW, UNCATEGORISED_ROW, 1, "#,##0.0"),
        number(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW, 2, "0.0%", "PERCENT"),
        number(FIRST_CATEGORY_ROW, UNCATEGORISED_ROW, 3, "#,##0"),
        # by month
        *section_title(MONTH_TITLE_ROW, 3), *column_header(MONTH_TITLE_ROW + 1, 3), banding(FIRST_MONTH_ROW, last_month, 3),
        fmt(FIRST_MONTH_ROW, last_month, 0, 1, {"numberFormat": {"type": "DATE", "pattern": "mmm yyyy"}}, "userEnteredFormat.numberFormat"),
        number(FIRST_MONTH_ROW, last_month, 1, "#,##0.0"), number(FIRST_MONTH_ROW, last_month, 2, "#,##0"),
        # by currency
        *section_title(CURRENCY_TITLE_ROW), *column_header(CURRENCY_TITLE_ROW + 1), banding(FIRST_CURRENCY_ROW, last_currency),
        number(FIRST_CURRENCY_ROW, last_currency, 1, "#,##0.0"), number(FIRST_CURRENCY_ROW, last_currency, 2, "#,##0"),
        number(FIRST_CURRENCY_ROW, last_currency, 3, "#,##0.0"),
        # exchange rates
        *section_title(RATES_TITLE_ROW, 3), *column_header(RATES_TITLE_ROW + 1, 3), banding(FIRST_RATE_ROW, LAST_RATE_ROW, 3),
        *editable(FIRST_RATE_ROW, LAST_RATE_ROW, 1),
        number(FIRST_RATE_ROW, LAST_RATE_ROW, 1, "0.00########"),
        fmt(FIRST_RATE_ROW, LAST_RATE_ROW, 2, 3, {"textFormat": {"italic": True, "foregroundColor": GREY, "fontSize": 9}}, "userEnteredFormat.textFormat"),
        # largest expenses
        *section_title(TOP_TITLE_ROW, 5), *column_header(TOP_TITLE_ROW + 1, 5), banding(FIRST_TOP_ROW, last_top, 5),
        fmt(FIRST_TOP_ROW, last_top, 0, 1, {"numberFormat": {"type": "DATE", "pattern": "dd/mm/yyyy"}}, "userEnteredFormat.numberFormat"),
        number(FIRST_TOP_ROW, last_top, 2, "#,##0.0"),
        fmt(FIRST_TOP_ROW, last_top, 4, 5, {"horizontalAlignment": "RIGHT", "textFormat": {"foregroundColor": GREY}}, "userEnteredFormat(horizontalAlignment,textFormat)"),
        # widths and gridlines
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 280}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 5}, "properties": {"pixelSize": 125}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 5, "endIndex": 6}, "properties": {"pixelSize": 30}, "fields": "pixelSize"}},
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True}}, "fields": "gridProperties.hideGridlines"}},
    ]
    _retry(lambda: store.spreadsheet.batch_update({"requests": requests}))


def _add_charts(store: SheetStore, sheet) -> None:
    sid = sheet.id

    def src(r1, r2, col):
        return {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2, "startColumnIndex": col, "endColumnIndex": col + 1}

    def position(row):
        return {"overlayPosition": {"anchorCell": {"sheetId": sid, "rowIndex": row - 1, "columnIndex": 6},
                                    "widthPixels": CHART_WIDTH, "heightPixels": CHART_HEIGHT}}

    title_format = {"bold": True, "fontSize": 14, "foregroundColor": INK}
    pie = {"addChart": {"chart": {
        "spec": {"title": "Share by category", "titleTextFormat": title_format, "backgroundColor": WHITE,
                 "pieChart": {"legendPosition": "RIGHT_LEGEND", "pieHole": 0.45,
                              "domain": {"sourceRange": {"sources": [src(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW - 1, 0)]}},
                              "series": {"sourceRange": {"sources": [src(FIRST_CATEGORY_ROW, CATEGORY_TOTAL_ROW - 1, 1)]}}}},
        "position": position(5),
    }}}
    columns = {"addChart": {"chart": {
        "spec": {"title": "Spend by month", "titleTextFormat": title_format, "backgroundColor": WHITE,
                 "basicChart": {"chartType": "COLUMN", "legendPosition": "NO_LEGEND",
                                "axis": [{"position": "BOTTOM_AXIS", "title": ""}, {"position": "LEFT_AXIS", "title": ""}],
                                "domains": [{"domain": {"sourceRange": {"sources": [src(FIRST_MONTH_ROW, FIRST_MONTH_ROW + MONTH_SLOTS - 1, 0)]}}}],
                                "series": [{"series": {"sourceRange": {"sources": [src(FIRST_MONTH_ROW, FIRST_MONTH_ROW + MONTH_SLOTS - 1, 1)]}},
                                            "targetAxis": "LEFT_AXIS", "color": GREEN}]}},
        "position": position(MONTH_TITLE_ROW),
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
