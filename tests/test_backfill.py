import json
from datetime import date

from backfill import import_messages, parse, parse_dump
from tests.conftest import at

SAMPLE = """Shiva ❤️, [1 Sep 2026 at 21:14:10]:
A101
2045


Hamed Shams, [3 Sep 2026 at 09:59:44]:
UBER to Metro Station
84 TL

Hamed Shams, [3 Sep 2026 at 13:23:00]:
295 TL

Shiva ❤️, [3 Sep 2026 at 15:36:34]:
Istanbul card 
400
"""


class StubStore:
    def __init__(self, last=date(2026, 9, 2), known=()):
        self.last, self.known, self.rows = last, set(known), []

    def last_recorded_date(self):
        return self.last

    def stored_message_ids(self):
        return set(self.known)

    def add_messages(self, rows):
        self.rows.extend(rows)


def test_macos_copy_format_is_parsed(settings):
    parsed = parse(SAMPLE, settings)
    assert [m.sender for m in parsed.messages] == ["Shiva", "Hamed", "Hamed", "Shiva"]
    assert parsed.messages[0].text == "A101\n2045" and parsed.messages[0].sent_at == at(2026, 9, 1, 21, 14).replace(second=10)
    assert all(m.message_id < 0 for m in parsed.messages) and len({m.message_id for m in parsed.messages}) == 4
    assert parsed.unparsed == []


def test_header_variants_are_tolerated(settings):
    variants = [
        "Hamed Shams, [08.09.2026 02:30]\nUBER",  # Telegram Desktop, no colon
        "Hamed Shams [8 Sep 2026 at 2:30 AM]\nUBER",  # no comma, 12-hour clock
        "Hamed Shams, [24 Jul 2026 at 21:46:10 (24 Jul 2026 at 23:45:01)]:\nUBER",  # edit time
        "Hamed Shams,  [2026-09-08 02:30] : \nUBER",  # stray spaces
    ]
    for text in variants:
        parsed = parse_dump(text, settings)
        assert len(parsed.messages) == 1 and parsed.messages[0].text == "UBER", text
    assert parse_dump(variants[2], settings).messages[0].edited_at == at(2026, 7, 24, 23, 45).replace(second=1)


def test_lines_before_the_first_header_are_reported_not_lost_silently(settings):
    parsed = parse_dump("some stray line\nHamed Shams, [3 Sep 2026 at 09:59:44]:\nCafe 225 TL", settings)
    assert len(parsed.messages) == 1 and parsed.unparsed == ["some stray line"]
    assert "1 line(s)" in parsed.problems()
    assert parse("nothing that looks like a header", settings).messages == []


def test_telegram_desktop_json_export_is_parsed(settings):
    export = {"messages": [
        {"id": 5, "type": "message", "date": "2026-08-01T10:00:00", "date_unixtime": "1785571200", "from": "Shiva ❤️", "text": ["Migros ", {"type": "bold", "text": "1.250"}]},
        {"id": 6, "type": "service", "date": "2026-08-01T10:00:00", "text": ""},
    ]}
    parsed = parse(json.dumps(export), settings)
    assert [(m.message_id, m.sender, m.text) for m in parsed.messages] == [(-5, "Shiva", "Migros 1.250")]


def test_import_skips_covered_days_and_duplicates_and_explains(settings):
    messages = parse(SAMPLE, settings).messages
    store = StubStore(known=[messages[1].message_id])
    result = import_messages(store, settings, messages)
    assert (result.found, result.imported, result.before_start, result.duplicates) == (4, 2, 1, 1)
    assert result.start == date(2026, 9, 3)
    text = result.describe()
    assert "Dismissed 1 dated before 03/09/2026" in text and "01/09/2026 21:14 Shiva: A101 | 2045" in text
    assert "Skipped 1 already queued" in text
    assert len(store.rows) == 2


def test_import_can_start_earlier_on_request(settings):
    messages = parse(SAMPLE, settings).messages
    result = import_messages(StubStore(), settings, messages, since=date(2026, 9, 1))
    assert result.imported == 4 and result.before_start == 0
