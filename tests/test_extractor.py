import pytest
from pydantic import TypeAdapter, ValidationError

from config import ConfigError
from extractor import DEFAULT_CATEGORIES, audit, build_batch, clean_categories, extract, load_prompt, render_prompt, result_model
from sheets import InboxMessage
from tests.conftest import at


def test_clean_categories_drops_placeholders_and_duplicates_and_falls_back():
    assert clean_categories(["Food", "food ", "", "Custom category 1", "Other"]) == ["Food", "Other"]
    assert clean_categories([]) == list(DEFAULT_CATEGORIES)
    assert clean_categories(["Custom category 2"]) == list(DEFAULT_CATEGORIES)


def test_result_model_limits_category_to_the_sheet_list():
    model = result_model(["Groceries", "Other"])
    schema = TypeAdapter(model).json_schema()
    assert schema["$defs"]["Transaction"]["properties"]["category"]["enum"] == ["Groceries", "Other"]
    assert "merged_into" in schema["$defs"]["MessageResult"]["required"]
    row = {"description": "A101", "amount": 300, "currency": "TRY", "category": "Groceries", "date": None}
    ok = model.model_validate({"results": [{"message_id": 1, "transactions": [row], "merged_into": None, "skip_reason": None, "needs_review": False, "note": None}]})
    assert ok.results[0].transactions[0].category == "Groceries"
    with pytest.raises(ValidationError):
        model.model_validate({"results": [{"message_id": 1, "transactions": [{**row, "category": "Eating Out"}], "merged_into": None, "skip_reason": None, "needs_review": False, "note": None}]})


def test_prompt_placeholders_are_filled(settings):
    prompt = render_prompt(load_prompt(settings), ["Groceries", "Mystery"])
    assert "{{" not in prompt
    assert '"1,154.5" = 1154.5' in prompt  # DECIMAL_SEPARATOR "." rules
    assert "- Groceries: supermarkets" in prompt and "- Mystery\n" in prompt
    assert "TRY, TOMAN, EUR, USD, GBP" in prompt


def test_unknown_default_currency_is_a_config_error(settings, monkeypatch):
    monkeypatch.setattr(settings, "default_currency", "XYZ", raising=False) if False else None
    from dataclasses import replace
    with pytest.raises(ConfigError, match="DEFAULT_CURRENCY"):
        load_prompt(replace(settings, default_currency="XYZ"))


def test_build_batch_wraps_each_message_with_its_metadata():
    messages = [InboxMessage(2, 41, 'Sam "S"', at(2026, 7, 24, 21, 46), at(2026, 7, 24, 23, 45), "Gratis\n266 TL", "pending")]
    batch = build_batch(messages)
    assert batch.startswith('<message id="41" sender="Sam \'S\'" sent="2026-07-24 21:46" edited="true">')
    assert batch.endswith("Gratis\n266 TL\n</message>")


def test_number_rules_follow_the_decimal_separator(settings):
    from dataclasses import replace
    assert '"1.154,5" = 1154.5' in load_prompt(replace(settings, decimal_separator=","))


def result(message_id, transactions=(), merged_into=None, skip_reason=None):
    return {"message_id": message_id, "transactions": list(transactions), "merged_into": merged_into,
            "skip_reason": skip_reason, "needs_review": False, "note": None}


def tx(description="Cafe", amount=385.0, date=None):
    return {"description": description, "amount": amount, "currency": "TRY", "category": "Other", "date": date}


def parsed(*results):
    return result_model(["Other"]).model_validate({"results": list(results)}).results


def test_audit_rejects_damaged_answers_and_keeps_good_ones():
    good, problems, suspicious = audit(parsed(
        result(50, [tx()]),
        result(51, [tx("026 kU", 617.7, date=",")]),  # the corruption seen in production
        result(99, [tx()]),  # a message that was never sent
        result(52, [tx()], merged_into=101736),
        result(50, [tx()]),  # answered twice
        result(53, [tx(amount=0)]),
    ), expected={50, 51, 52, 53})
    assert [r.message_id for r in good] == [50] and suspicious == {}
    assert len(problems) == 5 and any("malformed date" in p for p in problems) and any("unknown message id 99" in p for p in problems)


def test_audit_flags_an_answer_that_is_cut_short():
    dump = "Sep 3\n------\nUBER ONE 250 TL\n\nSep 8\n------\nHavale 1,158.4 TL\n\nPortakal su 160 TL\n\nUBER 174 TL\n\nLunch 370 TL\n\nA101 722 TL"
    good, problems, suspicious = audit(parsed(result(52, [tx("UBER ONE", 250)])), expected={52}, texts={52: dump})
    assert good and problems == [] and "only 1 transaction(s)" in suspicious[52]
    complete = result(52, [tx(f"item {i}", 100) for i in range(6)])
    assert audit(parsed(complete), {52}, {52: dump})[2] == {}
    recap = result(4, skip_reason="spending recap")
    assert audit(parsed(recap), {4}, {4: "we spent 10,871 in 36 days, 120 per day"})[2] == {}  # a reason given: not suspicious


class FakeClient:
    """Answers the first call with a damaged, incomplete result and the retry with a complete one."""

    def __init__(self, answers):
        self.answers, self.requests = list(answers), []

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def parse(self, **request):
            self.outer.requests.append(request)
            payload = self.outer.answers.pop(0)
            parsed_output = request["output_format"].model_validate({"results": payload})
            return SimpleNamespace(parsed_output=parsed_output, stop_reason="end_turn", usage=SimpleNamespace(input_tokens=100, output_tokens=50))

    @property
    def messages(self):
        return self._Messages(self)


def test_extract_retries_once_without_thinking_and_reports_the_rest(settings):
    from types import SimpleNamespace as NS
    messages = [InboxMessage(2, 50, "Alex", at(2026, 9, 8, 12), None, "Cafe 385", "pending"),
                InboxMessage(3, 51, "Alex", at(2026, 9, 8, 13), None, "A101 300", "pending"),
                InboxMessage(4, 52, "Sam", at(2026, 9, 8, 14), None, "Uber 100", "pending")]
    client = FakeClient([
        [result(50, [tx()]), result(51, [tx("026 kU", 617.7, date=",")])],  # first answer: one good, one damaged, one missing
        [result(51, [tx("Groceries - A101", 300)])],  # retry answers only 51
    ])
    out = extract(client, settings, load_prompt(settings), messages, ["Other"])
    assert [r.message_id for r in out.results] == [51, 50]  # the retry's answers first, then the surviving good one
    assert out.unanswered == [52] and out.calls == 2 and out.input_tokens == 200
    assert client.requests[0]["output_config"] == {"effort": "high"} and client.requests[1]["output_config"] == {"effort": "low"}
    assert "thinking" not in client.requests[1]
    assert any("malformed date" in p for p in out.problems)


def test_extract_retries_a_truncated_answer_and_keeps_the_flag_if_it_stays_short(settings):
    dump = "Sep 3\n------\nUBER ONE 250 TL\n\nSep 8\n------\nHavale 1,158.4 TL\n\nPortakal su 160 TL\n\nUBER 174 TL\n\nLunch 370 TL\n\nA101 722 TL"
    messages = [InboxMessage(2, 52, "Alex", at(2026, 9, 16, 17), None, dump, "pending")]
    complete = [result(52, [tx(f"item {i}", 100) for i in range(6)])]
    client = FakeClient([[result(52, [tx("UBER ONE", 250)])], complete])
    out = extract(client, settings, load_prompt(settings), messages, ["Other"])
    assert out.calls == 2 and len(out.results[0].transactions) == 6 and out.suspicious == {}
    client = FakeClient([[result(52, [tx("UBER ONE", 250)])], [result(52, [tx("UBER ONE", 250)])]])
    out = extract(client, settings, load_prompt(settings), messages, ["Other"])
    assert out.calls == 2 and 52 in out.suspicious


from types import SimpleNamespace  # noqa: E402  (used by FakeClient)
