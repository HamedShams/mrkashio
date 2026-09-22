"""Claude does the fuzzy part: which messages are expenses, how many, amount, currency, category.

The deterministic parts (dates, row placement, formatting) live in `sync.py` and `sheets.py`.
Claude answers in plain JSON, which is validated here against the same Pydantic schema (categories limited
to the sheet's own list). The API's schema-enforced output mode is deliberately not used: with long
reasoning it produced answers that were syntactically valid but cut short or garbled, while plain JSON at
the same effort came back complete every time it was tested.

Every answer is audited: a result for an unknown message, an unreadable description, a malformed date,
a missing message, or far fewer items than the text visibly contains means the answer is incomplete or
damaged, and the batch is asked once more at the same effort. Messages that still look incomplete are
flagged, never trusted blindly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

import anthropic
from pydantic import BaseModel, ValidationError, create_model

from config import ConfigError, Settings
from sheets import InboxMessage

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).with_name("prompt.md")
MAX_MESSAGES_PER_CALL = 60  # keeps one answer well inside the output budget even when every message lists several items
MAX_OUTPUT_TOKENS = 64000  # streamed, so a long batch plus its reasoning never hits an HTTP timeout
JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
A_LETTER = re.compile(r"[^\W\d_]")  # any letter in any script
CURRENCY_WORDS = r"(?:tl|try|₺|lira|lir|€|eur|euros?|\$|usd|dollars?|£|gbp|pounds?|toman|tuman|لیر|لیره|تومان|تومن|یورو|دلار|پوند)"
# What the household writes as an amount: a number alone on its line (with an optional currency word, "++" or a remark in
# parentheses), or a number followed by a currency word. Postal codes, coordinates and dates inside prose do not count.
AMOUNT_LIKE = re.compile(
    r"^[ \t]*(?:\+\+\s*)?[€$£₺]?\s*\d[\d.,]*\s*(?:k|bin|m)?\s*" + CURRENCY_WORDS + r"?\s*(?:\(.*\))?[ \t]*$"
    r"|(?<![\w.,])\d[\d.,]*\s*(?:k|bin|m)?\s*" + CURRENCY_WORDS + r"(?![\w])",
    re.IGNORECASE | re.MULTILINE,
)
ARITHMETIC = re.compile(r"\d[\d.,]*(?:\s*[-+*/×x=]\s*\d[\d.,]*)+")  # "2192-1200 = 992": one amount, not three
MONEY_BACK = "++"  # an amount written "++ 971" came back to the household and is stored negative
MAX_PLAUSIBLE_AMOUNT = 1e12

# Currencies the spreadsheet accepts in the currency column. TOMAN is Iranian toman, kept as the household writes it.
Currency = Literal["TRY", "TOMAN", "EUR", "USD", "GBP"]
CURRENCIES: tuple[str, ...] = get_args(Currency)

# How people write each currency, for commands that take one (/report €, /report lira). Plain lookup, no model involved.
CURRENCY_ALIASES: dict[str, tuple[str, ...]] = {
    "TRY": ("try", "tl", "₺", "lira", "liras", "lir", "turkish lira", "türk lirası", "turk lirasi", "لیر", "لیره"),
    "EUR": ("eur", "€", "euro", "euros", "یورو"),
    "USD": ("usd", "$", "us$", "dollar", "dollars", "us dollar", "us dollars", "dolar", "دلار"),
    "GBP": ("gbp", "£", "pound", "pounds", "sterling", "pound sterling", "پوند"),
    "TOMAN": ("toman", "tomans", "tuman", "تومان", "تومن"),
}


def parse_currency(text: str) -> str | None:
    """"€", "euro", "EURO", "eur", "US Dollar", "dollars", "lira", "tl" → the currency code, or None when unknown."""
    key = " ".join(text.replace(".", " ").split()).casefold()
    if not key:
        return None
    for code, aliases in CURRENCY_ALIASES.items():
        if key == code.casefold() or key in aliases or key.replace(" ", "") in {a.replace(" ", "") for a in aliases}:
            return code
    return None


# Categories come from the spreadsheet's own dropdown on the category column (SheetStore.category_options), so
# the sheet stays the single source of truth. This list is used only when the sheet defines none.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Groceries", "Eating Out", "Transport", "Housing & Utilities",
    "Health & Personal Care", "Shopping", "Leisure & Travel", "Subscriptions", "Other",
)

# One-line hints shown to Claude next to a category name, keyed by lower-case name. Covers the defaults
# above and the names in Google's budget template; a name without a hint is shown bare.
CATEGORY_HINTS: dict[str, str] = {
    # the nine defaults
    "groceries": "supermarkets, markets, bakeries, water and other food for home (A101, Migros, Şok, BİM)",
    "eating out": "restaurants, cafes and coffee chains (a Starbucks receipt is Eating Out even when it includes coffee capsules), bars, takeaway, a food order (the food itself; a separate delivery fee is Transport)",
    "transport": "moving people or things: taxi, Uber, bus, metro, Istanbulkart, fuel, parking, tolls and exit fees, airline baggage and overweight fees, courier, shipping and delivery fees (Lieferung, kargo)",
    "housing & utilities": "rent, deposit, building fees (aidat), the place the household lives in while settling somewhere (an Airbnb or short-term flat and its extensions), electricity, water, gas and the home internet (WiFi) bill, home supplies, furniture, repairs; mobile lines are Subscriptions",
    "health & personal care": "pharmacy, doctor, dentist, hospital, tests, health insurance, barber, hairdresser, cosmetics, hygiene, gym",
    "shopping": "clothes, shoes, electronics, gifts, malls and general retail not covered elsewhere",
    "leisure & travel": "going out and going away: cinema, concerts, events, hobbies, games, holiday hotels, flights, tours, trips; never a recurring service, and not the flat or Airbnb the household lives in",
    "subscriptions": "recurring paid services that are not a home utility: mobile data packages and SIM top-ups (Turkcell), app and software subscriptions (Cursor, ChatGPT, Apple One, Spotify, Netflix), memberships such as Uber One",
    "other": "money transfers to people (havale, Überweisung), bank and government charges, documents, services, anything that fits nowhere else",
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
    "transportation": "moving people or things: taxi, Uber, public transport, fuel, parking, courier, shipping and delivery fees",
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
    suspicious: dict[int, str] = field(default_factory=dict)  # message id → why its (retried) answer still looks incomplete

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


def output_instructions(schema: type[SyncResult]) -> str:
    """Appended to the system prompt: answer as one JSON object matching this schema, nothing else."""
    return "\n\n# Output schema\n\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)


def parse_answer(text: str, schema: type[SyncResult]) -> list[MessageResult] | None:
    """The JSON object in Claude's text, validated; None when there is none or it does not fit the schema."""
    body = JSON_FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return list(schema.model_validate_json(body[start : end + 1]).results)
    except (ValidationError, ValueError):
        return None


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


def audit(
    results: Sequence[MessageResult], expected: set[int], texts: dict[int, str] | None = None
) -> tuple[list[MessageResult], list[str], dict[int, str]]:
    """Keep the results that make sense; say what was wrong with the others; flag answers that look cut short.

    A damaged answer (seen in production: descriptions like "026 kU", a date of ",", a merged_into pointing
    at a message that does not exist) must never reach the sheet. A truncated one (one transaction returned for
    a message that visibly lists thirteen amounts) is kept but reported as suspicious, so the caller can retry
    and, if it stays that way, refuse to act on it destructively.
    """
    good: list[MessageResult] = []
    problems: list[str] = []
    suspicious: dict[int, str] = {}
    seen: set[int] = set()
    for result in results:
        why = _problem_with(result, expected, seen, (texts or {}).get(result.message_id, ""))
        if why:
            problems.append(why)
            continue
        seen.add(result.message_id)
        good.append(result)
        short = looks_truncated(result, (texts or {}).get(result.message_id, ""))
        if short:
            suspicious[result.message_id] = short
    return good, problems, suspicious


def looks_truncated(result: MessageResult, text: str) -> str | None:
    """A message that lists many amounts but came back with far fewer transactions, and no reason why."""
    if result.skip_reason or result.merged_into is not None:
        return None
    amounts = len(AMOUNT_LIKE.findall(ARITHMETIC.sub("0", text)))
    if amounts >= 3 and len(result.transactions) * 2 < amounts:
        return f"only {len(result.transactions)} transaction(s) for a text that lists about {amounts} amounts"
    return None


def _problem_with(result: MessageResult, expected: set[int], seen: set[int], text: str = "") -> str | None:
    if result.message_id not in expected:
        return f"a result for unknown message id {result.message_id}"
    if result.message_id in seen:
        return f"message {result.message_id} answered twice"
    if result.merged_into is not None and result.merged_into not in expected:
        return f"message {result.message_id} merged into unknown message {result.merged_into}"
    for transaction in result.transactions:
        if not A_LETTER.search(transaction.description):
            return f"message {result.message_id}: unreadable description {transaction.description!r}"
        if not 0 < abs(transaction.amount) < MAX_PLAUSIBLE_AMOUNT:
            return f"message {result.message_id}: implausible amount {transaction.amount}"
        if transaction.amount < 0 and MONEY_BACK not in text:
            return f"message {result.message_id}: negative amount {transaction.amount} without a '{MONEY_BACK}' marker in the message"
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
    system_prompt = render_prompt(prompt_template, categories) + output_instructions(schema)
    out = Extraction(results=[])
    for start in range(0, len(messages), MAX_MESSAGES_PER_CALL):
        chunk = messages[start : start + MAX_MESSAGES_PER_CALL]
        expected = {m.message_id for m in chunk}
        texts = {m.message_id: m.text for m in chunk}
        good, problems, suspicious = audit(_ask(client, settings, system_prompt, chunk, schema, out, retry=False), expected, texts)
        missing = expected - {r.message_id for r in good}
        if problems or missing or suspicious:
            log.warning("Claude's answer was incomplete or damaged (%d problem(s), %d unanswered, %d cut short); "
                        "asking once more", len(problems), len(missing), len(suspicious))
            out.problems.extend(problems)
            out.problems.extend(f"message {mid}: {why}" for mid, why in suspicious.items())
            retried, problems, still = audit(_ask(client, settings, system_prompt, chunk, schema, out, retry=True), expected, texts)
            out.problems.extend(problems)
            answered = {r.message_id for r in retried}
            good = retried + [r for r in good if r.message_id not in answered]  # the retry wins where it answered
            missing = expected - {r.message_id for r in good}
            suspicious = {mid: why for mid, why in still.items()} | {mid: why for mid, why in suspicious.items() if mid not in answered}
        out.results.extend(good)
        out.unanswered.extend(sorted(missing))
        out.suspicious.update(suspicious)
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
    """One streamed API call at the configured effort; the retry is simply a second try at the same effort."""
    with client.messages.stream(
        model=settings.anthropic_model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": build_batch(chunk)}],
        output_config={"effort": settings.anthropic_effort},
    ) as stream:
        response = stream.get_final_message()
    out.calls += 1
    out.input_tokens += response.usage.input_tokens
    out.output_tokens += response.usage.output_tokens
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    results = parse_answer(text, schema)
    if results is None:
        log.warning("Claude's answer was not a valid JSON object for the schema (stop_reason=%s, %d characters)%s",
                    response.stop_reason, len(text), " on the retry" if retry else "")
        out.problems.append("the answer was not a valid JSON object for the schema")
        return []
    return results
