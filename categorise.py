"""Fill in or re-check categories: `python bot.py categorise [--rows 5:152] [--all] [--dry-run]`.

Rows of the transactions tab whose category cell is empty and whose description is not are sent to Claude
once (plain JSON at the configured effort, validated locally, categories limited to the sheet's own list) and
nothing but those category cells is written, each re-checked to be still empty right before the write. With
`--all`, every row that has a description is re-checked and only the cells whose category would change are
written, so a new or redefined category can be applied to the whole history. Lines without a description (a
note typed across a row, a blank line) are left alone. The one paid call is the same kind the sync makes; a
dry run prints the proposed categories and writes nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError, create_model

from config import Settings
from extractor import CATEGORY_HINTS, JSON_FENCE, MAX_OUTPUT_TOKENS
from sheets import SheetStore, _as_date, _retry

log = logging.getLogger(__name__)

MAX_ROWS_PER_CALL = 300

SYSTEM = """You assign a spending category to rows of a household's expense spreadsheet.

Each row has a row number, a date, an amount with its currency, and a free-text description written by the household in English, German, Turkish or Persian. Pick exactly one category per row from this list, judging by what was paid for (the service, not the place):
{{CATEGORIES}}

- Judge foreign words by meaning: "Lieferung" and "kargo" are delivery fees (Transport), "kira" is rent, "eczane" is a pharmacy, "havale" is a money transfer.
- A delivery fee is Transport even when food was delivered; the food itself is Eating Out.
- Money transfers to people, deposits, exchange fees, bank charges and rows that record an event rather than a purchase take the category for fees, transfers and everything else.
- Answer every row, in the order given, and never invent a row number.
"""


@dataclass
class Candidate:
    row: int
    date: str
    amount: object
    currency: str
    description: str
    current: str = ""  # the category the row has now (empty when it has none)


@dataclass
class Plan:
    candidates: list[Candidate]
    mapping: dict[int, str] = field(default_factory=dict)
    unanswered: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for category in self.mapping.values():
            counts[category] = counts.get(category, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    def changes(self) -> dict[int, str]:
        """The rows whose category would actually change."""
        current = {c.row: c.current for c in self.candidates}
        return {row: category for row, category in self.mapping.items() if current.get(row, "") != category}

    def describe(self) -> str:
        by_row = {c.row: c for c in self.candidates}
        rechecking = any(c.current for c in self.candidates)
        lines = [f"{len(self.candidates)} rows {'re-checked' if rechecking else 'without a category'}; Claude answered {len(self.mapping)}; "
                 f"{len(self.changes())} would change."]
        for row in sorted(self.changes()):
            c = by_row[row]
            was = f"{c.current} → " if c.current else ""
            lines.append(f"  G{row}: {was}{self.mapping[row]:<24} ← {c.date} · {c.amount} {c.currency} · {c.description}")
        if self.counts():
            lines.append("Per category: " + ", ".join(f"{name} {n}" for name, n in self.counts().items()))
        if self.unanswered:
            lines.append(f"No usable answer for rows {', '.join(map(str, self.unanswered))}; run again for them.")
        for problem in self.problems:
            lines.append(f"Problem: {problem}")
        return "\n".join(lines)


def answer_model(categories: Sequence[str]) -> type[BaseModel]:
    row_model = create_model("CategoryRow", row=(int, ...), category=(Literal[tuple(categories)], ...))  # type: ignore[valid-type]
    return create_model("CategoryAnswer", rows=(list[row_model], ...))


def candidates(store: SheetStore, first_row: int, last_row: int, everything: bool = False) -> list[Candidate]:
    """Rows in the range with a description and (unless `everything`) no category. Notes across a row have no description and are skipped."""
    c = store.columns
    first, last = min(c.written, key=c.index), max(c.written, key=c.index)
    offset = {name: c.index(getattr(c, name)) - c.index(first) for name in ("date", "amount", "currency", "description", "category")}
    width = c.index(last) - c.index(first) + 1
    values = _retry(lambda: store.target.get_values(f"{first}{first_row}:{last}{last_row}", value_render_option="UNFORMATTED_VALUE"))
    found: list[Candidate] = []
    for index, row in enumerate(values):
        row = list(row) + [""] * (width - len(row))
        description, category = str(row[offset["description"]]).strip(), str(row[offset["category"]]).strip()
        if not description or (category and not everything):
            continue
        when = _as_date(row[offset["date"]])
        found.append(Candidate(first_row + index, when.strftime("%d/%m/%Y") if when else str(row[offset["date"]]),
                               row[offset["amount"]], str(row[offset["currency"]]).strip(), description, category))
    return found


def plan(client: anthropic.Anthropic, settings: Settings, rows: Sequence[Candidate], categories: Sequence[str]) -> Plan:
    """Ask Claude once per chunk for the category of every row; ask once more for whatever came back unusable."""
    model = answer_model(categories)
    lines = [f"- {name}: {CATEGORY_HINTS[name.lower()]}" if name.lower() in CATEGORY_HINTS else f"- {name}" for name in categories]
    system = (SYSTEM.replace("{{CATEGORIES}}", "\n".join(lines))
              + "\n\n# Output\n\nReply with one JSON object and nothing else, no code fences: "
              '{"rows": [{"row": <row number>, "category": <a name from the list>}, ...]}, one entry per row. '
              "It must validate against this JSON schema:\n" + json.dumps(model.model_json_schema(), ensure_ascii=False))
    result = Plan(list(rows))
    for start in range(0, len(rows), MAX_ROWS_PER_CALL):
        chunk = rows[start : start + MAX_ROWS_PER_CALL]
        expected = {c.row for c in chunk}
        answered = _ask(client, settings, system, chunk, model, result)
        missing = expected - set(answered)
        if missing:
            log.warning("%d rows came back without a usable category; asking once more", len(missing))
            answered |= _ask(client, settings, system, [c for c in chunk if c.row in missing], model, result)
        result.mapping.update({row: category for row, category in answered.items() if row in expected})
        result.unanswered.extend(sorted(expected - set(result.mapping)))
    return result


def _ask(client: anthropic.Anthropic, settings: Settings, system: str, chunk: Sequence[Candidate], model: type[BaseModel], out: Plan) -> dict[int, str]:
    text = "\n".join(f"row {c.row} · {c.date} · {c.amount} {c.currency} · {c.description}" for c in chunk)
    with client.messages.stream(
        model=settings.anthropic_model, max_tokens=MAX_OUTPUT_TOKENS, system=system,
        messages=[{"role": "user", "content": text}], output_config={"effort": settings.anthropic_effort},
    ) as stream:
        response = stream.get_final_message()
    out.calls += 1
    out.input_tokens += response.usage.input_tokens
    out.output_tokens += response.usage.output_tokens
    answer = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    body = JSON_FENCE.sub("", answer.strip())
    first, last = body.find("{"), body.rfind("}")
    if first == -1 or last == -1:
        out.problems.append("the answer held no JSON object")
        return {}
    try:
        parsed = model.model_validate_json(body[first : last + 1])
    except (ValidationError, ValueError) as exc:
        out.problems.append(f"the answer did not fit the schema: {str(exc).splitlines()[0]}")
        return {}
    return {entry.row: entry.category for entry in parsed.rows}


def apply(store: SheetStore, mapping: dict[int, str], expected: dict[int, str] | None = None) -> str:
    """Write the category cells, and only those, after checking that each still holds what was read (empty, or the value
    in `expected` when re-checking). Returns the range written."""
    if not mapping:
        return ""
    column = store.columns.category
    first, last = min(mapping), max(mapping)
    current = _retry(lambda: store.target.get_values(f"{column}{first}:{column}{last}"))
    for row in mapping:
        cell = current[row - first] if row - first < len(current) else []
        value = str(cell[0]).strip() if cell else ""
        if value != (expected or {}).get(row, ""):
            raise RuntimeError(f"refusing to write: {column}{row} holds {value!r}, not what was read a moment ago; nothing was written")
    updates = [{"range": f"{column}{row}", "values": [[category]]} for row, category in sorted(mapping.items())]
    _retry(lambda: store.target.batch_update(updates, value_input_option="RAW"))
    return f"{column}{first}:{column}{last}"
