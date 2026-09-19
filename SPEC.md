# Kashio — Telegram expense notes to Google Sheets

Reference specification for the current code. Anything specific to one deployment (bot name, spreadsheet, chat ids, hosting) lives only in that deployment's environment variables and is not described here.

---

## 1. What it does, in one paragraph

Kashio is a small always-on Python service on Railway. It sits in your Telegram group and saves every message it sees into a hidden `Bot_Inbox` tab of your spreadsheet the moment it arrives. This costs nothing: the Google Sheets API is free and no AI is involved. Twice a month (1st and 15th at 09:00 Istanbul, one env var to change), or when one of you types `/sync`, it takes every unprocessed message, sends the whole batch to Claude Sonnet 5 in **one API call**, and appends the resulting rows (date, amount, currency, description, category) under the last row of the target tab. It then posts a one-line summary in the group, sends the full run report to your private chat with the bot, records the same report in a `Bot_Runs` tab, and marks the inbox rows processed. Setup is one command: a group admin types `/setup` in the group and the bot stores the pairing in a hidden `Bot_Config` tab. Non-expense messages are skipped; anything ambiguous is flagged, not guessed.

**The invariant you care about:** the Anthropic API is called from exactly one place, the sync.
- A **scheduled** sync calls Claude only if at least `SCHEDULED_MIN_MESSAGES` (default 5) messages are pending. Below that it skips, logs the skip, and tells you.
- **`/sync`** calls Claude if at least `MANUAL_MIN_MESSAGES` (default 1) is pending. Otherwise it answers "Nothing new to process" and makes no call.
- Writing in the group never triggers an AI call.

---

## 2. Architecture

```
Telegram group (the household)                  Your private chat with the bot
        │  long polling                                ▲ full run report, every run
        ▼                                              │
┌──────────────────────────────────────────────────────┴───────┐
│  Kashio · one Python process on Railway (~60 MB RAM)         │
│                                                              │
│  on every message / edit ──► upsert raw text into            │
│  (free, no AI)               "Bot_Inbox" tab  (pending)      │
│                                                              │
│  on SYNC_CRON (≥5 pending)                                   │
│  or /sync (≥1 pending) ────► read pending inbox rows         │
│  (the only AI call)          ONE call to Claude Sonnet 5     │
│                              (structured JSON output)        │
│                              append rows to target tab B:E,G │
│                              mark inbox rows processed       │
│                              log the run in "Bot_Runs"       │
│                              summary in the group            │
└──────────────────────────────────────────────────────────────┘
        ▲                                         ▲
   Telegram Bot API                        Google Sheets API
   (bot token, Railway variable)           (service account, Railway variable)
```

### Why always-on, and why that does not cost you AI money

Telegram bots cannot read chat history, and Telegram discards undelivered updates after 24 hours. Something must listen continuously or messages are lost. The listener only copies text into the sheet. The expensive part, Claude, runs on the schedule.

### Storage: three sheet tabs plus Telegram, no database, no file

| Need | Where it lives | Why |
|---|---|---|
| Unprocessed and processed messages | `Bot_Inbox` tab (hidden) with a status column | Survives redeploys, visible, free |
| The transactions tab itself, when missing | Created with a header row (Date, Amount, Currency, Description, By, Category) | A fresh spreadsheet works on first run |
| Run history: when, trigger, messages, rows added, sheet range, skipped, flagged, tokens, estimated cost, error | `Bot_Runs` tab (hidden) **and** a Telegram message to you | The sheet is a third party and can be down; the Telegram copy is built in memory first and is sent even when every sheet write failed |
| Which group to record, who gets reports | `Bot_Config` tab (hidden), written by `/setup`; env vars override | Pair from Telegram, no redeploy |
| Settings | Railway environment variables, all with defaults except the three secrets | Change one, redeploy |
| Which spreadsheet | `GOOGLE_SHEET_ID`, or found through the Drive API among the spreadsheets shared with the service account | One less id to hunt for |
| Debug logs | Railway's log viewer (stdout) | Kept for days, searchable |
| Telegram's "which updates have I seen" pointer | Telegram's own servers | Re-delivered after a restart; the inbox de-duplicates by message id |

A file "as a mini database" is the one option that fails on Railway: the filesystem is wiped on every deploy and restart unless you add a paid volume. If a real need appears later, a Railway volume or Postgres can be added without changing the design.

### Who decides what

| Claude (fuzzy judgement) | Code (deterministic) |
|---|---|
| Is this message an expense at all? | Date, from the message timestamp |
| How many expenses are in it? | Day rollover before 04:00 |
| Amount as a number, currency, category (from the sheet's own list) | Which row to write to, formats, formula-safe text, answer audit and retry |
| Light typo fix, emoji removal, "Groceries - A101" prefix | Never insert the same message twice; thresholds; one sync at a time |
| Flag anything ambiguous | Retries, logging, the summary, the run log, the report |

---

## 3. Model, thinking effort and cost

### Model: Claude Sonnet 5 (`claude-sonnet-5`), $2 per million input tokens, $10 per million output tokens

Haiku 4.5 ($1 / $5) would handle most messages, but its failure mode is a silent wrong row in a finance sheet, and the price difference is a few cents a month. Opus 5 ($5 / $25) is overkill. `ANTHROPIC_MODEL` is an env var.

### Thinking effort: `high`, and why the API's schema-enforced output is not used

On 19 Sep 2026 a ten-message batch at `high` came back with results for only two messages: the model reasoned correctly about all ten (visible in the thinking summary) but the JSON it then wrote degenerated after about 1,800 characters into garbage (`"date":","`, a description `"026 kU"`, a `merged_into` of 101736) and stopped; the API's schema-enforced output mode (`output_config.format`) kept it syntactically valid, so it parsed as a success. Reproduced three times. The same afternoon the edited version of that message came back with one transaction and the revision logic replaced twelve rows with one. The decisive test came later that day: the identical batch at `high` with the schema-enforced mode switched off, asking for plain JSON in text and validating it locally against the same Pydantic schema, came back complete and coherent twice (10 results, 24 items, message 52 with all 13). The fault was the constrained output mode interacting with long reasoning, not the reasoning. Decisions: Claude answers in plain JSON (the schema is appended to the prompt, the answer is streamed, code fences tolerated, validated with the same model, categories still limited to the sheet's list); effort stays `high` for both the first attempt and the retry; `extractor.audit()` still rejects results for unknown ids, duplicates, `merged_into` outside the batch, unreadable descriptions, implausible amounts and malformed dates, and flags an answer as *cut short* when a message lists at least three amounts but came back with fewer than half as many transactions and no reason; any rejection, missing message, invalid JSON or cut-short answer triggers one retry; whatever is still unanswered stays pending, whatever still looks cut short is flagged instead of trusted. For an edited message, `sync._hold_reason` refuses to touch its rows when the new answer looks cut short, when Claude is unsure and returned fewer items than the rows already written, or when the count fell below half; the message is flagged with "rows left unchanged" and the operator edits again to retry.

### Your traffic, priced

Assumptions from your numbers: 50 transactions and 4 noise messages per 10 days, so per month **150 transactions in about 120 messages, plus 12 noise messages, 132 messages total**. Token estimates: system prompt plus schema about 1,700 tokens per call; about 45 input tokens per message; about 35 output tokens per message plus 30 per transaction; thinking about 20 tokens per message at `medium`, 50 at `high`, 120 at `xhigh`.

| Schedule | Calls / month | Input tokens | Output tokens incl. thinking | `medium` | `high` | `xhigh` | Pessimistic (×3 of high) |
|---|---|---|---|---|---|---|---|
| **Twice a month (1st, 15th) — default** | 2 | ~9,300 | ~11,800 – 25,000 | ~$0.14 | **~$0.18** | ~$0.30 | ~$0.55 |
| Weekly (Monday) | 4 | ~12,700 | ~11,800 – 25,000 | ~$0.14 | ~$0.18 | ~$0.30 | ~$0.55 |

Per run: about 7 cents twice a month, or about 3.5 cents weekly. A `/sync` with nothing pending makes no API call. Roughly **$2 a year**. Weekly costs almost the same as twice-monthly because output tokens scale with your transactions, not with the number of runs. No prompt caching and no Batches API: both would save fractions of a cent per call in exchange for code.

Safety net: set a monthly spend limit of $5 in the Anthropic console (Settings → Limits).

### The call (as implemented in `extractor.py`)

```python
response = client.messages.parse(
    model=settings.anthropic_model,           # claude-sonnet-5
    max_tokens=16000,
    system=system_prompt,                     # prompt.md with categories and currencies filled in
    messages=[{"role": "user", "content": build_batch(chunk)}],
    output_format=SyncResult,                 # Pydantic model → JSON schema enforced by the API
    output_config={"effort": settings.anthropic_effort},
)
```

Chunks of at most 150 messages per call. Token usage from `response.usage` goes into the run log.

---

## 4. Behaviour spec (as implemented)

### Pairing (`/setup`)

- A group admin types `/setup` in the group. The bot checks the admin role via Telegram, stores `group_id`, `admin_id` (the admin's user id, which is also their private chat id) and names in `Bot_Config`, replies, and tries to message the admin privately; if Telegram refuses (Start never pressed), it says so in the group.
- `TELEGRAM_CHAT_ID` / `TELEGRAM_ADMIN_CHAT_ID` environment variables override the stored pairing. A second group cannot take over an existing pairing without clearing `Bot_Config`.

### Ingest (continuous, free)

- Accept messages only from the paired group. Everything else is ignored.
- Until paired, the bot logs group messages with a hint to run `/setup` and records nothing. `/start` explains the state in any chat.
- New message (text or photo caption): append a row to `Bot_Inbox` with `message_id, sender, sent_at, edited_at, text, status=pending`.
- Edited message: still pending, the text is replaced. Skipped or flagged without rows: reopened as `pending`. Already turned into rows: `pending_revision`; at the next sync Claude re-extracts the new text and `replace_transactions` updates the message's rows in place, removes extra ones (bottom-up, each re-checked for its provenance note right before deletion) and appends missing ones. Edited into a note or emptied: a retraction, its rows are removed without asking Claude. Rows are found through the note `kashio:<message id>` on each date cell, so only rows the bot wrote are ever touched. Telegram does not report deletions; the README tells users to edit instead.
- Sheets write fails: retry 3 times with backoff, then reply "couldn't save this message; edit it to retry".

### Sync (on `SYNC_CRON` or `/sync`)

1. One sync at a time. `/sync` during a run replies "already running".
2. Read all `pending` rows from `Bot_Inbox`. Compare with the threshold for the trigger. Below it: status `skipped_threshold`, logged and reported, no API call.
3. One call to Claude with `prompt.md` as system prompt and the schema in §5. Messages folded into another message's transaction (an amount sent separately, a correction) come back with `merged_into` set and get status `merged` in the inbox.
4. For every returned transaction: `date = transaction.date or rollover(message.sent_at)`; `rollover` moves anything before `DAY_ROLLOVER_HOUR` (04:00) to the previous day. Descriptions that start with `=`, `+`, `-` or `@` get a leading apostrophe so the spreadsheet never reads them as formulas.
5. Find the last used row of the target tab: the highest row with any value in B, C or E (pre-filled "TRY" cells in D do not count).
6. **Guardrail:** re-read the destination rows and refuse to write if any of B, C, E, F or G already holds data (only the pre-filled default currency in D is tolerated). Then copy the formatting of the last row onto the new rows (borders, ₺ and date formats, the column-F dropdown) and write B:E and G in one batch with `USER_ENTERED`; dates are written as ISO strings, which every sheet locale parses as a date. Column F is never written.
7. Mark inbox rows `processed`, `merged`, `skipped` or `needs_review` with a timestamp and rows added.
8. Append one row to `Bot_Runs`: time, trigger, requested by, status, pending, processed, rows added, sheet range, skipped, needs review, input tokens, output tokens, cost, model, effort, error.
9. Send the full report to `TELEGRAM_ADMIN_CHAT_ID` (or the group if unset). If `POST_SUMMARY=true`, post a one-line summary in the group.
10. Any failure: status `failed`, rows stay `pending`, the report still goes out with the error, and if even the run log could not be written the report says so.

### Command line

```
python bot.py check           verify Telegram, Sheets, Anthropic and the schedule; no paid call
python bot.py sync --dry-run  call Claude, print the rows, write nothing, message nobody
python bot.py sync            one real sync from the terminal
python bot.py backfill FILE   queue older messages from a paste (.txt) or a Telegram Desktop export (.json)
python bot.py                 run the bot (what Railway runs)
```

### Telegram commands

| Command | Where | Who | Effect |
|---|---|---|---|
| `/setup` | group | group admin | pair the bot with the group and the admin |
| `/sync` (also `@botname /sync`) | group or private | group members | process everything pending; summary in the group, full report in private |
| `/backfill` … (`/done` / `/cancel`) | private | group members | paste older messages; Telegram splits long pastes into several messages, so the import starts 20 s after the last part or at once on `/done`; the reply lists every dismissed message; then a sync runs |
| `/status`, `/start`, `/help` | anywhere | anyone | integration checklist with fixes, and the command list |

### Messy input the prompt handles

- A description and its amount in two consecutive messages from the same sender ("UBER", then "10 TL") are one transaction, attached to the description message; the amount-only message is skipped with the reason "amount for message <id>". Verified live.
- Currency words in Turkish, German, English and Persian (TL, ₺, lira, لیر, تومان, euro, dollar, یورو, دلار, پوند) and Persian digits.
- Corrections to an earlier message in the same batch are applied to that message.

### Cold start and history

Telegram bots never receive messages sent before they joined. History comes in through `/backfill` (paste) or `backfill FILE`, which skip everything dated on or before the sheet's last recorded day and anything already stored, and queue the rest as pending. The earlier *live* cutoff rule, which refused any row dated before the sheet's last entry at every sync, was removed on 19 Sep 2026: it rejected a legitimate multi-day dump posted late (message 52, "Sep 3 … Sep 15"), because the sheet's last date came from the bot's own previous run, not from complete hand entry.

### Categories across languages (19 Sep 2026)

"2€ lieferung" was filed under Eating Out because the category hints named "food delivery" under Eating Out and nothing about delivery under Transport, so the model matched the German word to the only delivery it had been given. Fix: the Transport hint now reads "moving people or things: … courier, shipping and delivery fees (Lieferung, kargo)", Eating Out is "a food order (the food itself; a separate delivery fee is Transport)", Other names money transfers (havale, Überweisung), and the prompt's Category section tells the model that descriptions come in English, German, Turkish or Persian, often as a single word, to judge by meaning with a few worked equivalences, and to categorise the service paid for rather than the place. One example ("2€ lieferung" → Transport) was added.

### The inbox as an audit trail

An edit never overwrites an inbox row: the previous row is marked `superseded` (its text, status and rows kept) and a new row is appended with the edited text and the status the next sync should act on (`pending`, `pending_revision`, or `skipped` for a note). A deletion marks the latest row `pending_deletion`, then `deleted`, text kept. So the chain of edits and deletions of any message can be read from the tab. Status changes made by a sync (`processed`, `merged`, `skipped`, `needs_review`) are written in place on the row they concern.

### The Summary report tab (`init-sheet`)

`python bot.py init-sheet` builds a report tab (`SUMMARY_TAB`, default `Summary`) over the transactions tab, all formulas, styled with green header bands, tinted column headers, alternating row shading, a donut chart and a column chart: spend, share and count per category, spend per month (months found with `SORT(UNIQUE(EOMONTH(…)))`), totals per currency, the ten largest expenses, a pie chart by category and a column chart by month. Totals count rows in a base currency (cell B3, prefilled from `DEFAULT_CURRENCY`); other currencies are listed separately, never summed together. The category names in `A7:A25` of that tab are what the category dropdown on the transactions tab offers, because `init-sheet` points the dropdown's data validation there, so a category is added by typing it in the next free cell. An existing tab of that name is kept unless `--rewrite` is given, in which case it is deleted and rebuilt (the transactions tab is never touched).

### Who can talk to the bot

Only the paired group is recorded; messages from any other chat are ignored, and while paired the bot leaves any other group it is added to (`my_chat_member` updates). `/sync` and `/backfill` are accepted only from members of the paired group (checked with Telegram), `/setup` only from a group admin and only while unpaired or from the fixed group; strangers in a private chat get a one-line refusal. The remaining exposure is the unpaired window of a fresh deployment and the token itself; the README recommends BotFather's `/setjoingroups` → Disable for a personal instance and `/revoke` if the token ever leaks.

### Reports

The private report is Telegram HTML: a bold header with a status emoji (💸 synced, 🧪 dry run, ⏭ nothing to do, ❌ failed), one bullet per figure, then bold sections with bullets for rejected answers, still-pending messages, held edits and items needing review; all user text is HTML-escaped. The group summary and the status checklist use the same style. The terminal gets the plain-text form of the same report.

### Retries

Anthropic calls: the SDK retries 408/409/429/5xx and connection errors up to 3 times with exponential backoff (0.5 s doubling to 8 s, honouring `retry-after`); a request that still fails fails the sync, messages stay pending, and the report carries the error. Damaged, invalid or cut-short answers get one more call at the same effort. Google Sheets calls: 3 attempts with 2/4 s backoff. Telegram sends: logged, never fatal.

### Number format and layout

`DECIMAL_SEPARATOR` (`.` default, `,` for 1.154,5) is rendered into the prompt's amount rules. `COLUMN_DATE/AMOUNT/CURRENCY/DESCRIPTION/CATEGORY` (defaults B, C, D, E, G) drive every read and write on the transactions tab, the header row of a tab the bot creates, the guardrail, the formats and the Summary's formulas; letters must be single and distinct.

### Guarantees and limits

- A message is inserted at most once (status column plus de-duplication by message id).
- Messages sent before the bot joined the group are invisible to it.
- Deleted messages: Telegram sends no event, so before each sync `Kashio.detect_deletions` calls `setMessageReaction(reaction=[])` on every live message that has rows in the sheet (`SheetStore.messages_with_rows`); an existing message answers `Reaction_empty`, a deleted one `Message to react not found` (verified live on 19 Sep 2026 with messages 73 and 78). Deleted ones are queued as `pending_deletion`; the sync removes their rows through the provenance note and marks them `deleted`, keeping text and history in the inbox. Safety: a "chat not found" aborts the check, unexpected answers are ignored, and if every probed message looks deleted the check is discarded. Deletions and note-only edits are applied even below the scheduled threshold; only Claude is gated by it.
- If Telegram upgrades your group to a supergroup, the chat id changes. Update the env var.

---

## 5. Data contract

Input to Claude (one per pending message):

```xml
<message id="1041" sender="Sam" sent="2026-07-24 21:46" edited="true">
Gratis
266 TL

Cafe 
385 TL
</message>
```

Output schema (enforced by the API; Pydantic on our side, all fields required):

```python
Currency = Literal["TRY", "TOMAN", "EUR", "USD", "GBP"]
Category = Literal["Groceries", "Eating Out", "Transport", "Housing & Utilities", "Health",
                   "Personal Care", "Shopping", "Leisure & Travel", "Fees & Services", "Other"]

class Transaction(BaseModel):
    description: str
    amount: float
    currency: Currency
    category: Category
    date: str | None              # YYYY-MM-DD only if the message names another day

class MessageResult(BaseModel):
    message_id: int
    transactions: list[Transaction]   # empty = not an expense
    skip_reason: str | None
    needs_review: bool
    note: str | None

class SyncResult(BaseModel):
    results: list[MessageResult]
```

Sheet columns written: **B** date, **C** amount (number), **D** currency, **E** description, **G** category. **A and F are untouched.**

### Categories (column G): read from the sheet at every sync

Column G carries a dropdown fed from the category table in `Summary!B28:B35`, which the Summary's SUMIF formulas also use. The bot imposes no list: at each sync it reads the dropdown's allowed values, drops template placeholders, builds the output schema with exactly those names, and lists them in the prompt with a one-line hint where one is known. Editing the Summary table is all it takes to change categories. On 8 Sep 2026 the list was reduced, with the operator's approval, to eight MECE names (the planned £750 stays on the housing row):

| Category | Covers |
|---|---|
| Groceries | supermarkets, markets, bakeries, water and other food for home |
| Eating Out | restaurants, cafes, coffee, bars, takeaway, delivery |
| Transport | taxi, Uber, Istanbulkart and public transport, fuel, parking |
| Housing & Utilities | rent, electricity, water, gas, internet, phone bills, home supplies, furniture (an IKEA desk), repairs |
| Health & Personal Care | pharmacy, doctor, dentist, hospital, tests, insurance, barber, cosmetics, hygiene, gym |
| Shopping | clothes, shoes, electronics, gifts, malls and general retail not covered elsewhere |
| Leisure & Travel | entertainment, cinema, concerts, subscriptions, hobbies, hotels, flights, tours, trips |
| Other | fees, bank and government charges, documents, services, anything that fits nowhere else |

If column G ever has no dropdown, this same list is the built-in fallback (`DEFAULT_CATEGORIES` in `extractor.py`). Adding a ninth row to the Summary table (for example "Fees & Services") is all it takes to split one out again.

---

## 6. What the bot would write for your sample

12 rows and 2 skips, the same 12 rows you produced by hand, now with a category. Four descriptions differ only where you added details by hand that are not in the Telegram message.

| Telegram message | Bot writes (B / C / D / E / G) | Your manual entry (E) | Difference |
|---|---|---|---|
| Gratis 266 TL (24 Jul 21:46) | 24/07/2026 · 266 · TRY · Gratis · Personal Care | same | — |
| Cafe 385 TL (same message, edited in) | 24/07/2026 · 385 · TRY · Cafe · Eating Out | Cafe (Turk Kahvesi) | detail added by hand |
| Avm 810 (second member) | 24/07/2026 · 810 · TRY · Avm · Shopping | avm (random stuffs for the hause) | detail added by hand |
| Cafe IKEA 350 TL | 25/07/2026 · 350 · TRY · Cafe IKEA · Eating Out | same | — |
| UBER 452 TL | 25/07/2026 · 452 · TRY · UBER · Transport | UBER nach hause (with luggages from Meka) | detail added by hand |
| A101 300 (26 Jul 01:28) | **25/07/2026** · 300 · TRY · Groceries - A101 · Groceries | Groceries - A101 (oil) | date via 04:00 rollover ✓; "(oil)" by hand |
| A101 851 TL | 26/07/2026 · 851 · TRY · Groceries - A101 · Groceries | same | — |
| Cafe 320 TL | 26/07/2026 · 320 · TRY · Cafe · Eating Out | Cafe (Turk Kahvesi) | detail added by hand |
| "…TOTAL of $10,871 USD…" | skipped: spending recap | skipped | ✓ |
| Cafe 435 TL | 28/07/2026 · 435 · TRY · Cafe · Eating Out | same | — |
| istanbul card charge 414 TL | 28/07/2026 · 414 · TRY · istanbul card charge · Transport | same | — |
| A101 100 TL | 28/07/2026 · 100 · TRY · Groceries - A101 · Groceries | same | — |
| 📅 @member | skipped: no expense | skipped | ✓ |
| Barbershop 💈 (arash) 604 TL | 29/07/2026 · 604 · TRY · Barbershop · Personal Care | Barbershop | ✓ emoji and name dropped |

If you want those extra details in the sheet, write them in the Telegram message ("Cafe (Turk Kahvesi) 385") and the bot keeps them verbatim.

---

## 7. Decisions (all settled)

- Name **Kashio**; the bot's privacy mode must be disabled in BotFather so it sees group messages.
- Scheduled sync twice a month (`SYNC_CRON=0 9 1,15 * *`), only when ≥ `SCHEDULED_MIN_MESSAGES` (5) are pending.
- `/sync` processes everything pending when ≥ `MANUAL_MIN_MESSAGES` (1); otherwise "nothing new".
- Thinking effort `high`.
- Messages stored in `Bot_Inbox`; runs in `Bot_Runs`; every run report also sent to your private chat with the bot. No database, no file.
- Column F (the "By" dropdown: member names) left empty as asked; column G gets a category read from the sheet's own dropdown, now the eight names above; column D one of TRY, TOMAN, EUR, USD, GBP.
- History: older messages come in through `/backfill` (paste) or `backfill FILE`, strictly after the last recorded day by default; live messages are written whatever their date.
- Guardrail: new rows only ever go below the last used row, after the destination cells are verified empty; rows the bot wrote are updated or removed only through their provenance note when their message is edited or deleted, each re-checked before deletion; other rows and columns are never touched.
- Pairing via `/setup`, stored in the sheet; env vars are optional overrides.
- Day rollover at 04:00. Grocery prefix list and name-dropping rule in `prompt.md`. Formatting copied from the previous row.
- Backfill: built, by paste in Telegram or from a file, because the bot cannot see history.

---

## 8. Files (flat, no subfolders)

```
kashio/
├── bot.py            Telegram handlers (/setup, /sync, /backfill…), schedule, command line
├── backfill.py       Parses pasted Telegram messages or a Desktop JSON export; queues them
├── summary.py        Builds the Summary report tab (init-sheet): formulas, charts, the category dropdown
├── tests/            Offline pytest suite (config, extractor, sync, sheets, backfill, bot routing)
├── LICENSE           MIT
├── .github/workflows/tests.yml   runs the tests on every push
├── requirements-dev.txt          requirements + pytest
├── sync.py           One sync run: thresholds, dates, rows, sheet writes, report text
├── extractor.py      The Claude call: schema, categories, currencies, prompt loading
├── sheets.py         Google Sheets: inbox, run log, appending rows, copying formats
├── config.py         Reads and validates every environment variable
├── prompt.md         The system prompt; edit rules, store names and examples here
├── requirements.txt  5 dependencies
├── railway.json      Start command and single replica for Railway
├── .python-version   3.12
├── .env.example      Every variable, documented
├── .gitignore        Keeps .env and key files out of GitHub
├── README.md         Setup, configuration, commands
└── SPEC.md           This document
```

Dependencies: `python-telegram-bot[job-queue]` (Telegram + scheduler), `gspread` (Sheets), `anthropic` (Claude), `pydantic` (schema), `python-dotenv` (local `.env`).

---

## 9. Security notes

- Secrets (bot token, Anthropic key, Google key) live only in Railway Variables. The repo has `.env.example` with placeholders and a `.gitignore` that excludes `.env` and `*.json` key files. Anyone with the repo cannot reach your sheet or your bot.
- The bot token was pasted into a chat. Rotating it is a 10-second job: BotFather → `/revoke` → choose the bot → copy the new token into Railway. Recommended before the first deploy.
- The service account can only edit spreadsheets you explicitly share with it.
- Descriptions are written formula-safe; the bot never writes outside columns B–E and G of the target tab and its own three hidden tabs.
- Append-only guardrail: destination rows are verified empty immediately before every write; no update or delete request is ever issued against existing rows of the target tab.
- `/backfill` is restricted to private chats and to members of the paired group; `/setup` to group admins.

---

## 10. Testing status

**Live, 8 Sep 2026, from a developer machine with real credentials:**
- `python bot.py check`: Telegram token, group and admin (from env), Sheets access, categories from the dropdown, Anthropic key and schedule all pass. Hidden tabs created; run-log header extended with `merged`.
- Ingest: the bot ran locally for 45 s and stored three pending group messages, including an edit ("test" → "UBER"); migration service messages were ignored.
- One sync (the only paid call): 3 pending → 1 row at `Transactions_Trip#2!B153:G153` as a real date 07/09/2026 (02:30 rolled back a day), numeric ₺10.0, TRY, "UBER", F empty, G "Transport"; "10 TL" folded into that row; the greeting skipped; inbox statuses and run log correct; report delivered. 4,600 input / 234 output tokens, $0.0115.
- Sheet discovery without `GOOGLE_SHEET_ID`: exercised; see the README for the Drive API requirement.

**Unit tests (`pytest`, offline, run in CI):** configuration defaults and every validation message, prompt rendering and the exact output schema (dynamic category enum, `merged_into`), date rollover, the cutoff rule, message pairing and inbox statuses, all report texts, the paste parser on the real samples and its tolerant variants, the JSON export parser, import filters with their explanations, `_as_date`, `last_used_row`, and the append-only guardrail.

**Live on Railway, 8 Sep 2026 15:50, first deploy:** the deployed bot consumed the nine updates queued at Telegram; three earlier `/sync` commands with nothing pending were answered "Nothing new to process" and logged as `skipped_threshold`; a fourth `/sync` processed "UBER 2 / 1,000.5 یورو" into row 154 (08/09/2026, 1000.5, EUR, Transport), so Persian currency words and comma-formatted amounts work end to end on the deployed code; a message edited after its sync was marked `edited_after_sync` with a warning, and the sheet left untouched. CI (GitHub Actions) green on the pushed commit.

**Verified live by 19 Sep 2026:** the scheduled trigger (15 Sep), the photo acknowledgement, manual `/sync` runs, an edit re-synced in place, a deleted message's row removed, the Summary tab built over live data, the plain-JSON extractor at `high` on the batch that used to fail (10 of 10 messages, 24 items, one call), and a week of production use.

**Not exercised live:** `/setup`, `/backfill` and `/status` typed in Telegram, the note acknowledgement, a Telegram delivery failure, the guardrail's refusal path, and the health endpoint under Railway.

## 11. Deploy to-do

1. Done: pushed over HTTPS with the `gh` credential helper, rebased onto GitHub's initial commit; Railway built and the bot is live. The healthcheck path is `/health`. Leave Railway's cron schedule empty.
2. Railway → Variables are already set (chat ids included), so `/setup` is not needed here; it exists for anyone else who deploys the project.
3. Done on first deploy: the queued `/sync` commands were answered and a test expense became row 154.
4. Optional: fill 30 Jul – 7 Sep by copying those messages from the Telegram chat and, in your private chat with the bot, `/backfill`, paste, wait 20 s or `/done`.
