import pytest
from pydantic import TypeAdapter, ValidationError

from config import ConfigError
from extractor import DEFAULT_CATEGORIES, build_batch, clean_categories, load_prompt, render_prompt, result_model
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
