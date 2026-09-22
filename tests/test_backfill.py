import json
from datetime import date

from backfill import _amounts_in, import_messages, parse, parse_dump, same_item
from sheets import STATUS_DUPLICATE, SheetEntry
from tests.conftest import at

SAMPLE = """Sam ❤️, [1 Sep 2026 at 21:14:10]:
A101
2045


Alex Doe, [3 Sep 2026 at 09:59:44]:
UBER to Metro Station
84 TL

Alex Doe, [3 Sep 2026 at 13:23:00]:
295 TL

Sam ❤️, [3 Sep 2026 at 15:36:34]:
Istanbul card 
400
"""


class StubStore:
    def __init__(self, known=(), texts=None, on_sheet=()):
        self.known, self.texts, self.on_sheet = set(known), dict(texts or {}), list(on_sheet)
        self.rows, self.held = [], []

    def stored_message_ids(self):
        return set(self.known)

    def stored_texts(self):
        return {text: list(times) for text, times in self.texts.items()}

    def transaction_index(self):
        return list(self.on_sheet)

    def add_messages(self, rows, status="pending"):
        (self.held if status == STATUS_DUPLICATE else self.rows).extend(rows)


def test_macos_copy_format_is_parsed(settings):
    parsed = parse(SAMPLE, settings)
    assert [m.sender for m in parsed.messages] == ["Sam", "Alex", "Alex", "Sam"]
    assert parsed.messages[0].text == "A101\n2045" and parsed.messages[0].sent_at == at(2026, 9, 1, 21, 14).replace(second=10)
    assert all(m.message_id < 0 for m in parsed.messages) and len({m.message_id for m in parsed.messages}) == 4
    assert parsed.unparsed == []


def test_header_variants_are_tolerated(settings):
    variants = [
        "Alex Doe, [08.09.2026 02:30]\nUBER",  # Telegram Desktop, no colon
        "Alex Doe [8 Sep 2026 at 2:30 AM]\nUBER",  # no comma, 12-hour clock
        "Alex Doe, [24 Jul 2026 at 21:46:10 (24 Jul 2026 at 23:45:01)]:\nUBER",  # edit time
        "Alex Doe,  [2026-09-08 02:30] : \nUBER",  # stray spaces
    ]
    for text in variants:
        parsed = parse_dump(text, settings)
        assert len(parsed.messages) == 1 and parsed.messages[0].text == "UBER", text
    assert parse_dump(variants[2], settings).messages[0].edited_at == at(2026, 7, 24, 23, 45).replace(second=1)


def test_lines_before_the_first_header_are_reported_not_lost_silently(settings):
    parsed = parse_dump("some stray line\nAlex Doe, [3 Sep 2026 at 09:59:44]:\nCafe 225 TL", settings)
    assert len(parsed.messages) == 1 and parsed.unparsed == ["some stray line"]
    assert "1 line(s)" in parsed.problems()
    assert parse("nothing that looks like a header", settings).messages == []


def test_telegram_desktop_json_export_is_parsed(settings):
    export = {"messages": [
        {"id": 5, "type": "message", "date": "2026-08-01T10:00:00", "date_unixtime": "1785571200", "from": "Sam ❤️", "text": ["Migros ", {"type": "bold", "text": "1.250"}]},
        {"id": 6, "type": "service", "date": "2026-08-01T10:00:00", "text": ""},
    ]}
    parsed = parse(json.dumps(export), settings)
    assert [(m.message_id, m.sender, m.text) for m in parsed.messages] == [(-5, "Sam", "Migros 1.250")]


def test_import_skips_what_the_inbox_has_by_id_or_by_text_and_time(settings):
    messages = parse(SAMPLE, settings).messages
    # the live copy of "Istanbul card 400" was stored at 15:35:59; Telegram's copy shows 15:36:34 (rounded, another minute)
    store = StubStore(known=[messages[1].message_id], texts={"istanbul card 400": [at(2026, 9, 3, 15, 35).replace(second=59)]})
    result = import_messages(store, settings, messages)
    assert (result.found, result.imported, result.before_start, result.duplicates, result.held) == (4, 2, 0, 2, [])
    assert [r[4] for r in store.rows] == ["A101\n2045", "295 TL"] and result.start is None
    assert "Skipped 2 already in the inbox" in result.describe() and "Dismissed" not in result.describe()


def test_the_same_text_on_the_same_day_at_another_time_is_held_for_a_person(settings):
    messages = parse(SAMPLE, settings).messages
    store = StubStore(texts={"istanbul card 400": [at(2026, 9, 3, 9, 0)]})  # six hours earlier: probably the same, maybe not
    result = import_messages(store, settings, messages)
    assert result.imported == 3 and len(store.held) == 1 and store.held[0][4] == "Istanbul card \n400"
    assert "recorded on this day at 09:00" in store.held[0][5] and "≈ recorded live at 09:00" in result.describe()
    # on another day the same text is simply a new message
    store = StubStore(texts={"istanbul card 400": [at(2026, 9, 2, 15, 36)]})
    assert import_messages(store, settings, messages).imported == 4


def test_a_message_repeating_a_sheet_row_of_the_same_day_is_held_not_queued(settings):
    messages = parse(SAMPLE, settings).messages
    on_sheet = [SheetEntry(161, date(2026, 9, 3), "UBER to Metro Station", 84, "TRY"),
                SheetEntry(140, date(2026, 9, 1), "Groceries - A101", 1999, "TRY")]  # same words, different amount: not a duplicate
    store = StubStore(on_sheet=on_sheet)
    result = import_messages(store, settings, messages)
    assert result.imported == 3 and len(result.held) == 1 and len(store.held) == 1
    assert [r[4] for r in store.rows] == ["A101\n2045", "295 TL", "Istanbul card \n400"]
    held = {r[4]: r[5] for r in store.held}
    assert "row 161: 03/09/2026 · UBER to Metro Station · ₺84" in held["UBER to Metro Station\n84 TL"]
    assert "same day, wording, amount and currency" in held["UBER to Metro Station\n84 TL"]
    text = result.describe()
    assert "Held back 1 that look already recorded" in text and "/review keep" in text and "≈ row 161" in text


def test_the_day_after_midnight_is_checked_against_both_calendar_days(settings):
    late = parse_dump("Alex, [4 Sep 2026 at 01:30:00]:\nkebab 300", settings).messages
    store = StubStore(on_sheet=[SheetEntry(170, date(2026, 9, 4), "kebab", 300, "TRY")])  # entered by hand under the 4th
    assert import_messages(store, settings, late).held and store.rows == []


def entry(description, amount, currency="TRY"):
    return SheetEntry(161, date(2026, 9, 3), description, amount, currency)


def test_same_item_needs_the_same_words_amount_and_currency():
    migros = entry("Groceries - Migros", 450)
    assert same_item("Migros 450 tl", migros, "TRY") and same_item("Migros 450", migros, "TRY")  # the default currency is TRY
    assert same_item("migros ₺450", migros, "TRY") and same_item("Migros ۴۵۰ لیر", migros, "TRY")
    assert not same_item("Migros 400 TL", migros, "TRY")  # another amount is another purchase
    assert not same_item("Migros 450", migros, "EUR")  # another default currency: not the same row
    assert not same_item("Migros 450 €", migros, "TRY")
    assert not same_item("Migros - Water 450 TL", migros, "TRY")  # the user's own wording counts, prefixes included
    assert not same_item("Migros", migros, "TRY") and not same_item("450 TL", migros, "TRY")
    assert same_item("Barbershop 604 TL", entry("Barbershop", 604), "TRY")
    assert same_item("Carre four\n1114", entry("Groceries - Carrefour", 1114), "TRY")  # spacing inside a name does not matter
    assert not same_item("Barbershop 💈 (arash)\n604 TL", entry("Barbershop", 604), "TRY")  # an extra word: not the same wording
    assert same_item("Migros 1.250", entry("Groceries - Migros", 1250), "TRY") and same_item("Havale 1,158.4 TL", entry("Havale", 1158.4), "TRY")
    assert same_item("taxi 350k toman", entry("taxi", 350000, "TOMAN"), "TRY")
    assert same_item("A101 674", entry("Groceries - A101", "₺674"), "TRY")  # an amount typed as text on the sheet
    assert not same_item("A101 674", entry("", "CHANGED €100 Euro to 5,000 TL"), "TRY")  # a note across the row, never a match


def test_a_multi_item_message_is_held_when_any_of_its_items_repeats_a_row(settings):
    text = "Gratis\n266 TL\n\nCafe\n385 TL"
    messages = parse_dump("Sam, [3 Sep 2026 at 15:00:00]:\n" + text, settings).messages
    assert import_messages(StubStore(on_sheet=[entry("Cafe", 385)]), settings, messages).held
    assert not import_messages(StubStore(on_sheet=[entry("Cafe", 380)]), settings, messages).held


def test_amounts_are_read_both_ways_a_separator_can_be_meant():
    assert _amounts_in("Migros 1.250") == {1.25, 1250.0} and _amounts_in("1,158.4 TL") == {1158.4} and _amounts_in("266,50") == {266.5}
    assert _amounts_in("2k") == {2000.0} and _amounts_in("A101 (oil) 300") == {300.0} and _amounts_in("1.234.567") == {1234567.0}


def test_import_can_dismiss_everything_before_a_day_on_request(settings):
    messages = parse(SAMPLE, settings).messages
    result = import_messages(StubStore(), settings, messages, since=date(2026, 9, 3))
    assert result.imported == 3 and result.before_start == 1 and result.start == date(2026, 9, 3)
    text = result.describe()
    assert "Dismissed 1 dated before 03/09/2026" in text and "01/09/2026 21:14 Sam: A101 | 2045" in text


def test_the_bots_own_messages_are_left_out_of_an_import(settings):
    dump = SAMPLE + "\nMr Kashio, [8 Sep 2026 at 21:35:46]:\n✅ Kashio synced 3 expense(s) from 3 message(s) (₺1,596).\n"
    parsed = parse(dump, settings, ignore_sender="Mr Kashio")
    assert len(parsed.messages) == 4 and parsed.ignored == 1 and parsed.notes() == ["Left out 1 message(s) written by the bot itself."]
    assert len(parse(dump, settings).messages) == 5  # without a name nothing is left out
    export = {"messages": [{"id": 5, "type": "message", "date": "2026-08-01T10:00:00", "from": "Mr Kashio", "text": "✅ report"},
                           {"id": 6, "type": "message", "date": "2026-08-01T10:00:00", "from": "Sam", "text": "Cafe 385"}]}
    parsed = parse(json.dumps(export), settings, ignore_sender="Mr Kashio")
    assert [m.text for m in parsed.messages] == ["Cafe 385"] and parsed.ignored == 1
