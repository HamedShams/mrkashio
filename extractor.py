"""Claude does the fuzzy part: which messages are expenses, how many, amount, currency, category.

The deterministic parts (dates, row placement, formatting) live in `sync.py` and `sheets.py`.
Every answer is audited: a result for an unknown message, an unreadable description, a malformed date,
or a missing message means the answer is incomplete or damaged, and the batch is asked again once
without extended thinking. Messages that still have no usable result are reported, never invented.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

import anthropic
from pydantic import BaseModel, create_model

from config import ConfigError, Settings
from sheets import InboxMessage

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).with_name("prompt.md")
MAX_MESSAGES_PER_CALL = 150
MAX_OUTPUT_TOKENS = 16000
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
A_LETTER = re.compile(r"[^\W\d_]")  # any letter in any script
MAX_PLAUSIBLE_AMOUNT = 1e12

# Currencies the spreadsheet accepts in the currency column. TOMAN is Iranian toman, kept as the household writes it.
Currency = Literal["TRY", "TOMAN", "EUR", "USD", "GBP"]
CURRENCIES: tuple[str, ...] = get_args(Currency)

# Categories come from the spreadsheet's own dropdown on the category column (SheetStore.category_options), so
# the sheet stays the single source of truth. This list is used only when the sheet defines none.
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

# How amounts are written, keyed by DECIMAL_SEPARATOR. Rendered into the prompt.
NUMBER_RULES = {
    ".": (
        '- Thousands may be grouped with a comma and decimals use a dot: "1,154" = 1154, "1,154.5" = 1154.5, '
        '"266.50" = 266.5, "1154" = 1154. A single separator followed by exactly three digits and nothing else '
        '("1.250" or "1,250") is a thousands separator: 1250.'
    ),
    ",": (
        '- Thousands may be grouped with a dot and decimals use a comma: "1.154" = 1154, "1.154,5" = 1154.5, '
        '"266,50" = 266.5, "1154" = 1154. A single separator followed by exactly three digits and nothing else '
        '("1.250" or "1,250") is a thousands separator: 1250.'
    ),
}


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


@dataclass
class Extraction:
    results: list[MessageResult]
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    unanswered: list[int] = field(default_factory=list)  # message ids with no usable result even after the retry
    problems: list[str] = field(default_factory=list)  # what was wrong with the answers that were rejected

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
    """Read prompt.md and fill in the currency list, the default currency and the number rules. Categories are filled per sync."""
    if settings.default_currency not in CURRENCIES:
        raise ConfigError(f"DEFAULT_CURRENCY must be one of {', '.join(CURRENCIES)}")
    return (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{{CURRENCIES}}", ", ".join(CURRENCIES))
        .replace("{{DEFAULT_CURRENCY}}", settings.default_currency)
        .replace("{{NUMBER_RULES}}", NUMBER_RULES[settings.decimal_separator])
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


def audit(results: Sequence[MessageResult], expected: set[int]) -> tuple[list[MessageResult], list[str]]:
    """Keep the results that make sense; say what was wrong with the others.

    A damaged answer (seen in production: descriptions like "026 kU", a date of ",", a merged_into pointing
    at a message that does not exist) must never reach the sheet.
    """
    good: list[MessageResult] = []
    problems: list[str] = []
    seen: set[int] = set()
    for result in results:
        why = _problem_with(result, expected, seen)
        if why:
            problems.append(why)
            continue
        seen.add(result.message_id)
        good.append(result)
    return good, problems


def _problem_with(result: MessageResult, expected: set[int], seen: set[int]) -> str | None:
    if result.message_id not in expected:
        return f"a result for unknown message id {result.message_id}"
    if result.message_id in seen:
        return f"message {result.message_id} answered twice"
    if result.merged_into is not None and result.merged_into not in expected:
        return f"message {result.message_id} merged into unknown message {result.merged_into}"
    for transaction in result.transactions:
        if not A_LETTER.search(transaction.description):
            return f"message {result.message_id}: unreadable description {transaction.description!r}"
        if not 0 < transaction.amount < MAX_PLAUSIBLE_AMOUNT:
            return f"message {result.message_id}: implausible amount {transaction.amount}"
        if transaction.date is not None and not DATE_PATTERN.fullmatch(transaction.date):
            return f"message {result.message_id}: malformed date {transaction.date!r}"
    return None


def extract(
    client: anthropic.Anthropic,
    settings: Settings,
    prompt_template: str,
    messages: Sequence[InboxMessage],
    categories: Sequence[str],
) -> Extraction:
    """One structured-output call per chunk of up to MAX_MESSAGES_PER_CALL messages, audited, retried once if needed."""
    schema = result_model(categories)
    system_prompt = render_prompt(prompt_template, categories)
    out = Extraction(results=[])
    for start in range(0, len(messages), MAX_MESSAGES_PER_CALL):
        chunk = messages[start : start + MAX_MESSAGES_PER_CALL]
        expected = {m.message_id for m in chunk}
        good, problems = audit(_ask(client, settings, system_prompt, chunk, schema, out, retry=False), expected)
        missing = expected - {r.message_id for r in good}
        if problems or missing:
            log.warning("Claude's answer was incomplete or damaged (%d problem(s), %d message(s) unanswered); "
                        "asking once more without extended thinking", len(problems), len(missing))
            out.problems.extend(problems)
            retried, problems = audit(_ask(client, settings, system_prompt, chunk, schema, out, retry=True), expected)
            out.problems.extend(problems)
            answered = {r.message_id for r in retried}
            good = retried + [r for r in good if r.message_id not in answered]  # the retry wins where it answered
            missing = expected - {r.message_id for r in good}
        out.results.extend(good)
        out.unanswered.extend(sorted(missing))
    return out


def _ask(
    client: anthropic.Anthropic,
    settings: Settings,
    system_prompt: str,
    chunk: Sequence[InboxMessage],
    schema: type[SyncResult],
    out: Extraction,
    *,
    retry: bool,
) -> list[MessageResult]:
    """One API call. The retry runs without extended thinking, which is where damaged answers were traced to."""
    request: dict = dict(
        model=settings.anthropic_model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": build_batch(chunk)}],
        output_format=schema,
    )
    if retry:
        request["thinking"] = {"type": "disabled"}
        request["output_config"] = {"effort": "high" if settings.anthropic_effort in ("xhigh", "max") else settings.anthropic_effort}
    else:
        request["output_config"] = {"effort": settings.anthropic_effort}
    response = client.messages.parse(**request)
    out.calls += 1
    out.input_tokens += response.usage.input_tokens
    out.output_tokens += response.usage.output_tokens
    if response.parsed_output is None:
        log.warning("Claude returned no structured output (stop_reason=%s)", response.stop_reason)
        return []
    return list(response.parsed_output.results)
