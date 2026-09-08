"""Claude does the fuzzy part: which messages are expenses, how many, amount, currency, category.

The deterministic parts (dates, row placement, formatting) live in `sync.py` and `sheets.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import anthropic
from pydantic import BaseModel, create_model

from config import ConfigError, Settings
from sheets import InboxMessage

PROMPT_PATH = Path(__file__).with_name("prompt.md")
MAX_MESSAGES_PER_CALL = 150
MAX_OUTPUT_TOKENS = 16000

# Currencies the spreadsheet accepts in column D. TOMAN is Iranian toman, kept as the household writes it.
Currency = Literal["TRY", "TOMAN", "EUR", "USD", "GBP"]
CURRENCIES: tuple[str, ...] = get_args(Currency)

# Categories come from the spreadsheet's own dropdown on column G (SheetStore.category_options), so the
# sheet stays the single source of truth. This list is used only when the sheet defines none.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Groceries", "Eating Out", "Transport", "Housing & Utilities",
    "Health & Personal Care", "Shopping", "Leisure & Travel", "Other",
)

# One-line hints shown to Claude next to a category name, keyed by lower-case name. Covers the defaults
# above and the names in Google's budget template; a name without a hint is shown bare.
CATEGORY_HINTS: dict[str, str] = {
    # the eight defaults
    "groceries": "supermarkets, markets, bakeries, water and other food for home (A101, Migros, Şok, BİM)",
    "eating out": "restaurants, cafes, coffee, bars, takeaway, food delivery",
    "transport": "taxi, Uber, Istanbulkart and public transport, fuel, parking",
    "housing & utilities": "rent, electricity, water, gas, internet, phone bills, home supplies, furniture, repairs",
    "health & personal care": "pharmacy, doctor, dentist, hospital, tests, health insurance, barber, hairdresser, cosmetics, hygiene, gym",
    "shopping": "clothes, shoes, electronics, gifts, malls and general retail not covered elsewhere",
    "leisure & travel": "entertainment, cinema, concerts, subscriptions, hobbies, hotels, flights, tours, trips",
    "other": "fees, bank and government charges, documents, services, anything that fits nowhere else",
    # finer names some sheets use
    "health": "pharmacy, doctor, dentist, hospital, tests, health insurance",
    "personal care": "barber, hairdresser, cosmetics and hygiene products, spa, gym",
    "leisure": "entertainment, cinema, concerts, subscriptions, hobbies, books, games",
    "travel": "flights, hotels, tours, trips away from home",
    "fees & services": "bank and card fees, government fees, visas and permits, documents, postage, education, professional services",
    # Google Sheets budget template
    "food": "groceries, supermarkets, restaurants, cafes, coffee, delivery",
    "gifts": "presents, flowers, donations",
    "health/medical": "pharmacy, doctor, dentist, hospital, health insurance",
    "home": "rent, furniture, home supplies, repairs, cleaning",
    "transportation": "taxi, Uber, Istanbulkart and public transport, fuel, parking",
    "personal": "barber, cosmetics and hygiene, clothes, hobbies, subscriptions",
    "pets": "pet food, vet, pet supplies",
    "utilities": "electricity, water, gas, internet, phone bills",
    "debt": "loan and credit-card repayments",
}
PLACEHOLDER_CATEGORY = re.compile(r"^custom category \d+$", re.IGNORECASE)


class Transaction(BaseModel):
    description: str
    amount: float
    currency: Currency
    category: str  # constrained to the sheet's exact category names at call time, see result_model()
    date: str | None  # YYYY-MM-DD, only when the message names a different day than it was sent


class MessageResult(BaseModel):
    message_id: int
    transactions: list[Transaction]  # empty when the message is not an expense
    merged_into: int | None  # id of the message this one was folded into (an amount-only message, a correction)
    skip_reason: str | None
    needs_review: bool
    note: str | None


class SyncResult(BaseModel):
    results: list[MessageResult]


class ExtractionError(RuntimeError):
    """Claude did not return a usable structured result."""


@dataclass
class Extraction:
    results: list[MessageResult]
    input_tokens: int
    output_tokens: int

    def cost_usd(self, settings: Settings) -> float:
        return (
            self.input_tokens * settings.price_input_per_million
            + self.output_tokens * settings.price_output_per_million
        ) / 1_000_000


def clean_categories(options: Sequence[str]) -> list[str]:
    """De-duplicate, drop template placeholders such as 'Custom category 1', fall back to the defaults."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for option in options:
        name = " ".join(str(option).split())
        if not name or PLACEHOLDER_CATEGORY.match(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        cleaned.append(name)
    return cleaned or list(DEFAULT_CATEGORIES)


def result_model(categories: Sequence[str]) -> type[SyncResult]:
    """The output schema with `category` limited to exactly the allowed names (enforced by the API)."""
    category_type = Literal[tuple(categories)]  # type: ignore[valid-type]
    transaction = create_model("Transaction", __base__=Transaction, category=(category_type, ...))
    message_result = create_model("MessageResult", __base__=MessageResult, transactions=(list[transaction], ...))
    return create_model("SyncResult", __base__=SyncResult, results=(list[message_result], ...))


def load_prompt(settings: Settings) -> str:
    """Read prompt.md and fill in the currency list and default currency. Categories are filled per sync."""
    if settings.default_currency not in CURRENCIES:
        raise ConfigError(f"DEFAULT_CURRENCY must be one of {', '.join(CURRENCIES)}")
    return (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{{CURRENCIES}}", ", ".join(CURRENCIES))
        .replace("{{DEFAULT_CURRENCY}}", settings.default_currency)
    )


def render_prompt(template: str, categories: Sequence[str]) -> str:
    lines = []
    for name in categories:
        hint = CATEGORY_HINTS.get(name.lower())
        lines.append(f"- {name}: {hint}" if hint else f"- {name}")
    return template.replace("{{CATEGORIES}}", "\n".join(lines))


def build_batch(messages: Sequence[InboxMessage]) -> str:
    """Wrap each raw message in a tag carrying its id, sender and local send time."""
    blocks = []
    for message in messages:
        sender = message.sender.replace('"', "'")
        edited = "true" if message.edited_at else "false"
        blocks.append(
            f'<message id="{message.message_id}" sender="{sender}" '
            f'sent="{message.sent_at:%Y-%m-%d %H:%M}" edited="{edited}">\n{message.text}\n</message>'
        )
    return "\n\n".join(blocks)


def extract(
    client: anthropic.Anthropic,
    settings: Settings,
    prompt_template: str,
    messages: Sequence[InboxMessage],
    categories: Sequence[str],
) -> Extraction:
    """One structured-output call per chunk of up to MAX_MESSAGES_PER_CALL messages."""
    schema = result_model(categories)
    system_prompt = render_prompt(prompt_template, categories)
    results: list[MessageResult] = []
    input_tokens = output_tokens = 0
    for start in range(0, len(messages), MAX_MESSAGES_PER_CALL):
        chunk = messages[start : start + MAX_MESSAGES_PER_CALL]
        response = client.messages.parse(
            model=settings.anthropic_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": build_batch(chunk)}],
            output_format=schema,
            output_config={"effort": settings.anthropic_effort},
        )
        if response.parsed_output is None:
            raise ExtractionError(f"no structured output returned (stop_reason={response.stop_reason})")
        results.extend(response.parsed_output.results)
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
    return Extraction(results=results, input_tokens=input_tokens, output_tokens=output_tokens)
