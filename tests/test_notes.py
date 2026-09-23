"""The private-note keyword: what is kept, what is dropped, and how imports treat it."""

from dataclasses import replace

from backfill import import_messages, parse
from sync import split_note
from tests.test_backfill import StubStore


def test_everything_from_the_keyword_to_the_end_is_dropped():
    assert split_note("A101 300 #note oil for the week", "#note") == ("A101 300", True)
    assert split_note("Gratis 266 TL\n#NOTE birthday present", "#note") == ("Gratis 266 TL", True)
    assert split_note("#note review the budget on Friday", "#note") == ("", True)


def test_keyword_must_be_a_whole_word_and_is_case_insensitive():
    assert split_note("bought a #notebook 120 TL", "#note") == ("bought a #notebook 120 TL", False)
    assert split_note("Cafe 385 #Note: with Ali", "#note") == ("Cafe 385", True)
    assert split_note("Cafe 385", "#note") == ("Cafe 385", False)
    assert split_note("rent 25000 NB private", "NB") == ("rent 25000", True)
    assert split_note("anything", "") == ("anything", False)


def test_imports_skip_note_only_messages_and_trim_mixed_ones(settings):
    settings = replace(settings, note_keyword="#note")
    dump = ("Sam, [3 Sep 2026 at 09:00:00]:\n#note we should check the budget\n\n"
            "Sam, [3 Sep 2026 at 10:00:00]:\nA101 300 #note oil for the week\n")
    messages = parse(dump, settings).messages
    store = StubStore()
    result = import_messages(store, settings, messages)
    assert result.notes == 1 and result.imported == 1
    assert store.rows[0][4] == "A101 300"  # the note part never reaches the inbox
    assert "Skipped 1 private notes" in result.describe()


def test_default_keyword_is_hash_note(settings):
    assert settings.note_keyword == "#note"
