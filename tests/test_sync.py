from datetime import date

from extractor import Extraction, result_model
from sheets import RUNS_HEADERS, InboxMessage
from sync import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED_THRESHOLD,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    RunReport,
    _apply,
    format_report,
    format_summary,
    parse_override_date,
    rollover_date,
    sanitize_description,
)
from tests.conftest import at

CATEGORIES = ["Groceries", "Eating Out", "Transport", "Other"]


def result(message_id, transactions=(), merged_into=None, skip_reason=None, needs_review=False, note=None):
    return {"message_id": message_id, "transactions": list(transactions), "merged_into": merged_into,
            "skip_reason": skip_reason, "needs_review": needs_review, "note": note}


def tx(description, amount, category="Other", currency="TRY", when=None):
    return {"description": description, "amount": amount, "currency": currency, "category": category, "date": when}


def parsed(*results):
    return result_model(CATEGORIES).model_validate({"results": list(results)}).results


def report_for(trigger=TRIGGER_MANUAL):
    r = RunReport(trigger, "Alex", at(2026, 9, 8, 9), "claude-sonnet-5", "high")
    r.categories = CATEGORIES
    return r


def test_rollover_moves_small_hours_to_the_previous_day():
    assert rollover_date(at(2026, 7, 26, 1, 28), 4) == date(2026, 7, 25)
    assert rollover_date(at(2026, 7, 26, 4, 0), 4) == date(2026, 7, 26)
    assert rollover_date(at(2026, 7, 25, 19, 58), 4) == date(2026, 7, 25)


def test_sanitize_neutralises_formula_prefixes_and_whitespace():
    assert sanitize_description("=SUM(A1)") == "'=SUM(A1)"
    assert sanitize_description("  Cafe   IKEA ") == "Cafe IKEA"
    assert sanitize_description("+90 sim card") == "'+90 sim card"


def test_override_date_is_parsed_or_ignored():
    assert parse_override_date("2026-07-28") == date(2026, 7, 28)
    assert parse_override_date("yesterday") is None and parse_override_date(None) is None


def test_apply_pairs_amount_messages_skips_noise_and_flags_doubt(settings):
    pending = [
        InboxMessage(2, 2, "Alex", at(2026, 9, 8, 2, 30), None, "UBER", "pending"),
        InboxMessage(3, 3, "Alex", at(2026, 9, 8, 2, 37), None, "10 TL", "pending"),
        InboxMessage(4, 4, "Alex", at(2026, 9, 8, 2, 47), None, "hi", "pending"),
        InboxMessage(5, 5, "Sam", at(2026, 9, 8, 12, 0), None, "Cafe", "pending"),
        InboxMessage(6, 6, "Sam", at(2026, 9, 8, 13, 0), None, "never answered", "pending"),
    ]
    results = parsed(
        result(2, [tx("UBER", 10, "Transport")]),
        result(3, merged_into=2, skip_reason="amount for message 2"),
        result(4, skip_reason="greeting"),
        result(5, needs_review=True, note="no amount given"),
    )
    report = report_for()
    marks = _apply(Extraction(results, 100, 50), pending, settings, report)
    # message 6 got no answer: it is not marked at all, so it stays pending for the next sync
    assert [(m.message_id, status) for m, status, _, _ in marks] == [(2, "processed"), (3, "merged"), (4, "skipped"), (5, "needs_review")]
    assert marks[1][3] == "merged into message 2: amount for message 2"
    assert [(r.date.isoformat(), r.amount, r.description, r.category) for r in report.rows] == [("2026-09-07", 10.0, "UBER", "Transport")]
    assert (report.skipped, report.merged, len(report.review)) == (1, 1, 1)


def test_money_back_marker_makes_the_row_negative_even_if_the_model_forgot(settings):
    pending = [InboxMessage(2, 60, "Alex", at(2026, 8, 19, 2), None, "💸💰 Lamp cancelled and refunded\n++ 971 TL\n\nCoffee\n300 TL", "pending")]
    report = report_for()
    _apply(Extraction(parsed(result(60, [tx("💸💰 Lamp cancelled and refunded", 971), tx("Coffee", 300)])), 1, 1), pending, settings, report)
    assert [r.amount for r in report.rows] == [-971.0, 300.0]


def test_late_posted_history_is_written_with_its_own_dates(settings):
    pending = [InboxMessage(2, 52, "Alex", at(2026, 9, 16, 17), None, "Sep 3\n------\nUBER ONE 250 TL", "pending")]
    report = report_for()
    marks = _apply(Extraction(parsed(result(52, [tx("UBER ONE Subscription", 250, "Transport", when="2026-09-03")])), 1, 1), pending, settings, report)
    assert [r.date.isoformat() for r in report.rows] == ["2026-09-03"] and marks[0][1] == "processed"


def test_summaries_read_well_in_every_state():
    ok = report_for(); ok.status = STATUS_OK; ok.processed = 3; ok.calls = 1
    assert format_summary(ok).startswith("✅ Kashio wrote nothing from 3 messages.")
    quiet_ok = report_for(); quiet_ok.status = STATUS_OK
    assert format_summary(quiet_ok).startswith("✅ Kashio made no Claude call this time.")
    quiet = report_for(TRIGGER_SCHEDULE); quiet.status = STATUS_SKIPPED_THRESHOLD; quiet.pending, quiet.threshold = 3, 5
    assert "below the minimum of 5" in format_summary(quiet)
    empty = report_for(); empty.status = STATUS_SKIPPED_THRESHOLD; empty.threshold = 1
    assert format_summary(empty).startswith("Nothing new to process")
    failed = report_for(); failed.status = STATUS_FAILED; failed.error = "APIError: 503"
    assert "failed: APIError: 503" in format_summary(failed) and "Error: APIError: 503" in format_report(failed)


def test_run_log_row_matches_the_header():
    assert len(report_for().as_row()) == len(RUNS_HEADERS)
