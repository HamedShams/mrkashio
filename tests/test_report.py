"""/report: the Summary tab's "By month" table computed in code, and @mention routing in the group."""

import asyncio
from datetime import date
from types import SimpleNamespace

from telegram import Chat, MessageEntity

import bot
from extractor import parse_currency
from sheets import SheetEntry
from summary import MonthTotal, format_monthly_report, monthly_totals
import summary


def test_currency_aliases_cover_symbols_words_and_languages():
    assert parse_currency("€") == parse_currency("euro") == parse_currency("EURO") == parse_currency("eur") == "EUR"
    assert parse_currency("TRY") == parse_currency("TL") == parse_currency("LIRA") == parse_currency("Lir") == parse_currency(" tl ") == "TRY"
    assert parse_currency("$") == parse_currency("USD") == parse_currency("US Dollar") == parse_currency("dollars") == parse_currency("دلار") == "USD"
    assert parse_currency("£") == parse_currency("pounds") == "GBP" and parse_currency("تومان") == parse_currency("Toman") == "TOMAN"
    assert parse_currency("xyz") is None and parse_currency("") is None


class StubStore:
    def __init__(self, entries):
        self.entries = entries

    def transaction_index(self):
        return list(self.entries)


ENTRIES = [
    SheetEntry(5, date(2026, 6, 18), "Flight", 58_029_000, "TOMAN"),
    SheetEntry(6, date(2026, 6, 21), "Rent", 1021, "EUR"),
    SheetEntry(7, date(2026, 6, 21), "Su", 95, "TRY"),
    SheetEntry(8, date(2026, 6, 25), "typed amount", "₺674", "TRY"),  # text amount: ignored, as the tab does
    SheetEntry(9, date(2026, 7, 3), "Card", 1000, ""),  # blank currency: the default one
    SheetEntry(10, date(2026, 7, 10), "Refund", -500, "TRY"),
    SheetEntry(11, date(2026, 7, 12), "Odd", 20, "GBP"),
]
RATES = {"USD": 1.0, "TRY": 0.02, "EUR": 1.1, "TOMAN": 0.000005}  # in USD, as the household's table; no GBP


def test_monthly_totals_follow_the_tab_arithmetic_and_report_what_cannot_convert(settings, monkeypatch):
    monkeypatch.setattr(summary, "read_rate_values", lambda store, settings: dict(RATES))
    months, notes = monthly_totals(StubStore(ENTRIES), settings, "TRY")
    assert [(m.month, m.count) for m in months] == [(date(2026, 6, 1), 3), (date(2026, 7, 1), 3)]
    assert months[0].total == 58_029_000 * 0.000005 / 0.02 + 1021 * 1.1 / 0.02 + 95  # 14,507.25 + 56,155 + 95
    assert months[1].total == 1000 - 500  # the GBP row is counted but not converted
    assert notes == ["1 rows in GBP left out: no rate for it in the Exchange rates table"]
    in_eur, _ = monthly_totals(StubStore(ENTRIES), settings, "EUR")
    assert round(in_eur[1].total, 4) == round(500 * 0.02 / 1.1, 4)
    none, notes = monthly_totals(StubStore(ENTRIES), settings, "GBP")
    assert none == [] and "no exchange rate for GBP" in notes[0]


def test_report_text_matches_the_tab_format():
    months = [MonthTotal(date(2026, 6, 1), 221832.94, 36), MonthTotal(date(2026, 7, 1), 330199.4, 122)]
    text = format_monthly_report(months, "TRY", [], as_html=True)
    assert text.startswith("<b>📊 Spending by month, in TRY</b>\n• Jun 2026 · ₺221,832.9 · 36 rows\n• Jul 2026 · ₺330,199.4 · 122 rows\n• Total · ₺552,032.3 · 158 rows")
    assert "/report €" in text and "<" not in text.replace("<b>", "").replace("</b>", "")
    assert "3,969.0 EUR" not in format_monthly_report([MonthTotal(date(2026, 6, 1), 3969.0, 36)], "EUR", [], False)  # € sign, not a suffix
    assert "€3,969.0" in format_monthly_report([MonthTotal(date(2026, 6, 1), 3969.0, 36)], "EUR", [], False)
    assert "TOMAN" in format_monthly_report([MonthTotal(date(2026, 6, 1), 1, 1)], "TOMAN", [], False)


def test_mentions_route_to_the_right_handler(settings, monkeypatch):
    calls = []
    for name in ("on_start", "on_sync", "on_status", "on_group_message", "on_backfill"):
        monkeypatch.setattr(bot, name, (lambda n: (lambda u, c: _record(calls, n)))(name))
    monkeypatch.setattr(bot, "on_review", lambda u, c, words=None: _record(calls, ("review", words)))
    monkeypatch.setattr(bot, "on_report", lambda u, c, words=None: _record(calls, ("report", words)))
    context = SimpleNamespace(bot=SimpleNamespace(username="kashio_bot"))
    for text, expected in (
        ("@kashio_bot", "on_start"), ("@kashio_bot hi there", "on_start"), ("@kashio_bot /start", "on_start"), ("@Kashio_Bot /help", "on_start"),
        ("@kashio_bot /sync", "on_sync"), ("@kashio_bot  /sync please", "on_sync"), ("@kashio_bot /status", "on_status"),
        ("@kashio_bot /report €", ("report", ["€"])), ("@kashio_bot /report US Dollar", ("report", ["US", "Dollar"])),
        ("@kashio_bot /review done 2", ("review", ["done", "2"])), ("please @kashio_bot /report", ("report", [])),
        ("@kashio_bot A101 300", "on_group_message"), ("@kashio_bot ۴۵۰ لیر نان", "on_group_message"),
    ):
        calls.clear()
        asyncio.run(bot.on_mention(SimpleNamespace(effective_message=SimpleNamespace(text=text)), context))
        assert calls == [expected], text


async def _record(calls, value):
    calls.append(value)


def test_the_mention_filter_only_fires_for_this_bot(settings):
    from telegram import Message, User
    from datetime import datetime, timezone
    from tests.test_bot import BOT
    def message(text, mention):
        m = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=-1, type=Chat.SUPERGROUP), from_user=User(id=7, first_name="A", is_bot=False),
                    text=text, entities=(MessageEntity(type=MessageEntity.MENTION, offset=text.index(mention), length=len(mention)),))
        m.set_bot(BOT)
        return m
    flt = bot.MentionsBot()
    assert flt.filter(message("@kashio_bot /report", "@kashio_bot")) is True
    assert flt.filter(message("Daroo 250 @Mirzaee_Shiva", "@Mirzaee_Shiva")) is False  # another member mentioned: an ordinary note


def test_report_command_and_mention_are_wired(settings):
    from tests.test_bot import first_matching_handler, update_for
    application = bot.build_application(SimpleNamespace(settings=settings))
    assert first_matching_handler(application, update_for("/report eur")).callback is bot.on_report
    from telegram import Message, Update, User
    from datetime import datetime, timezone
    from tests.test_bot import BOT
    text = "@kashio_bot /sync"
    m = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=-1, type=Chat.SUPERGROUP), from_user=User(id=7, first_name="A", is_bot=False),
                text=text, entities=(MessageEntity(type=MessageEntity.MENTION, offset=0, length=len("@kashio_bot")),
                                     MessageEntity(type=MessageEntity.BOT_COMMAND, offset=12, length=5)))
    m.set_bot(BOT)
    assert first_matching_handler(application, Update(update_id=1, message=m)).callback is bot.on_mention
