# Kashio

A Telegram bot that turns the expense notes a household posts in a group chat into clean rows in a Google Sheet, using one Claude call per sync.

You keep writing what you already write: `A101 851 TL`, `Cafe 385`, `Uber 452`. Kashio stores every message the moment it arrives, and twice a month (or when you type `/sync`) it sends the whole batch to Claude, which decides which messages are expenses, splits messages that contain several, reads amounts and currencies, fixes small typos, assigns a category, and returns structured rows. Kashio appends them under your last row and reports back in Telegram.

## How it works

```
Telegram group ──► Kashio (Python, always on) ──► Bot_Inbox tab        (every message, free)
                                                 │
                        SYNC_CRON or /sync ──────┤ one Claude call ──► Transactions tab (B:E, G)
                                                 └──────────────────► Bot_Runs tab + Telegram report
```

- **Ingest is free.** Storing a message writes one row to a hidden inbox tab in your spreadsheet. No AI is involved.
- **Claude is called in exactly one place**, the sync. A scheduled sync only runs when at least `SCHEDULED_MIN_MESSAGES` messages are pending (default 5). `/sync` runs whenever at least `MANUAL_MIN_MESSAGES` is pending (default 1) and otherwise replies "nothing new".
- **Code decides the deterministic parts**: the date comes from the message timestamp (notes sent before `DAY_ROLLOVER_HOUR` count for the previous day), rows go under the last used row, formatting is copied from the row above, and a message is never inserted twice.
- **Every run is recorded twice**: a row in the hidden `Bot_Runs` tab (tokens, cost, rows added, errors) and the same report as a Telegram message, so you have it even when the spreadsheet is unreachable.
- **Messy input is expected.** A description and its amount split across two consecutive messages ("UBER", then "10 TL") are paired into one transaction. Turkish, German, English and Persian currency words (TL, ₺, لیر, تومان, euro, dollar…) and Persian digits are understood. Corrections to an earlier message in the same batch are applied.
- **Nothing is guessed.** Messages Claude cannot resolve (no amount, unknown currency, a correction to an older message) are flagged `needs_review` in the inbox and listed in the report.
- **Every answer is checked.** Claude answers in plain JSON that the bot validates against its own schema (categories limited to your sheet's list), then audits: a result for a message that was never sent, an unreadable description, a malformed date, a missing message, or far fewer items than the text visibly lists. If anything is off, the batch is asked once more; whatever still has no usable answer stays pending, and an answer that still looks cut short is flagged rather than trusted. Nothing invented ever reaches the sheet.
- **It never touches rows it did not write.** New rows go below the last used row, after a check that the destination cells are empty. Rows the bot wrote carry a small provenance note, and only those are ever updated or removed, when their Telegram message is edited or deleted. See Guardrails.
- **It tells you what is missing.** The bot starts with nothing but a Telegram token. Whatever else is absent or broken (the Google key, a spreadsheet that is not shared, the Anthropic key, the group pairing) is reported in plain words to whoever talks to it, with the fix, by `/status`, `/start` and any command that cannot run. No model is involved in that: plain checks and prewritten sentences.
- **Private notes stay private.** Anything from the word `#note` to the end of a message is a note for the humans: it is never stored, never sent to Claude, and a message that is only a note leaves nothing but a “[note]” acknowledgement in the inbox. The word is configurable (`NOTE_KEYWORD`).
- **Photos, voice messages and files are never touched.** Only text is read. A photo without a caption is acknowledged in the inbox as “[photo]”, skipped, and never downloaded, uploaded or sent to Claude. A caption under a photo or file is treated as the message text, and the inbox notes that a file came with it.

## Project layout

| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers (`/setup`, `/sync`, `/backfill`…), the schedule, the `/health` endpoint, and the command line (`run`, `check`, `sync`, `backfill`). |
| `backfill.py` | Parses pasted Telegram messages or a Telegram Desktop JSON export and queues them. |
| `sync.py` | One sync run: thresholds, dates, rows, sheet writes, and the report text. |
| `extractor.py` | The Claude call: output schema (Pydantic), categories, currencies, prompt loading. |
| `sheets.py` | Everything that touches Google Sheets: inbox, run log, appending rows. |
| `config.py` | Reads and validates every environment variable. |
| `prompt.md` | The system prompt. Edit rules, store names and examples here, no code needed. |
| `requirements.txt`, `railway.json`, `.python-version` | Dependencies and Railway start command. |
| `.env.example` | Every variable, documented. Copy to `.env` for local runs. |
| `tests/` | Offline unit tests (`pytest`); `requirements-dev.txt` installs them; `.github/workflows/tests.yml` runs them on every push. |

## Setup

### 1. Telegram bot (5 minutes)

1. In [@BotFather](https://t.me/BotFather): `/newbot`, choose a name and username, copy the token.
2. Still in BotFather: `/setprivacy` → your bot → **Disable**. Without this, bots in groups only see commands.
3. Add the bot to your group.

### 2. Google service account (10 minutes, once)

A service account is a robot identity with its own e-mail address. No extra Gmail account is needed.

1. [console.cloud.google.com](https://console.cloud.google.com): create a project (any name).
2. APIs & Services → Library → **Google Sheets API** → Enable.
   Then tell the bot which spreadsheet is yours, one of two ways: paste its id into `GOOGLE_SHEET_ID` (the long part of the URL between `/d/` and `/edit`), or also enable the **Google Drive API** in the same project and leave `GOOGLE_SHEET_ID` empty; the bot then finds the spreadsheet you shared with the service account by itself. If it cannot, its message says which of the two to do.
3. IAM & Admin → Service Accounts → Create → name it → Done.
4. Open it → Keys → Add key → Create new key → JSON. Download the file.
5. Open your spreadsheet → Share → paste the service account's e-mail (ends in `.iam.gserviceaccount.com`) → **Editor**.

The bot creates what it needs: the transactions tab (with a header row) if `SHEET_TAB` does not exist yet, and its three hidden tabs (`Bot_Inbox`, `Bot_Runs`, `Bot_Config`). An existing tab must have these columns: **B** date, **C** amount, **D** currency, **E** description, **G** category (a dropdown; its values become the allowed categories). Columns A and F are never written.

### 3. Anthropic API key

Create a key at [console.anthropic.com](https://console.anthropic.com). Setting a monthly spend limit there (for example $5) is a good safety net; a household produces well under $1 a month.

### 4. Deploy on Railway

1. Push this repository to GitHub and create a Railway service from it. Railway detects Python and runs `python bot.py` (from `railway.json`, which also sets the healthcheck path `/health`). Do **not** set a Railway cron schedule: the bot is an always-on service with its own scheduler (`SYNC_CRON`).
2. In the service's **Variables**, add the three secrets: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY` and `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the key file as a single line). Everything else has a default. Add `GOOGLE_SHEET_ID` if you did not enable the Drive API (or if several spreadsheets are shared with the service account), and `SHEET_TAB` if your tab is not called `Transactions_Trip#2`. Secrets live only here, never in the repo.

### 5. Pair the bot with your group (one minute, no redeploy)

1. Add the bot to your group. As a group admin, type `/setup` there. The bot stores the group's id and your id in a hidden `Bot_Config` tab of the spreadsheet and starts recording.
2. Open a private chat with the bot and press **Start**, so Telegram lets it send you the full sync reports. (`/setup` tells you if this is still needed.)

From now on every message in the group is stored. Type `/sync` to process what is pending, or wait for the schedule. If you prefer fixed configuration, set `TELEGRAM_CHAT_ID` and `TELEGRAM_ADMIN_CHAT_ID` as variables instead; they override `/setup`.

### 6. Loading older messages (optional)

Telegram bots never receive messages sent before they joined, even when the group's history is visible to new members. Two ways to bring older notes in, both free until the sync runs:

- **Paste them to the bot.** In a private chat with the bot, send `/backfill`, then paste the messages copied from the Telegram chat (select messages → Copy). Telegram cuts anything longer than 4,096 characters into several messages by itself, so the bot cannot know when the last part has arrived: it waits 20 seconds after the last part, or starts at once when you send `/done`. `/cancel` discards. You can also put the paste right after `/backfill` in one message; if that message is long enough to have been cut, the bot waits for the rest the same way. Messages written by the bot itself (its reports and replies) are left out. The bot queues the messages and runs a sync immediately.
- **From a file.** `python bot.py backfill result.json` for a Telegram Desktop export (chat menu → Export chat history → JSON), or a `.txt` with the copied messages. Then `python bot.py sync --dry-run` and `python bot.py sync`.

An import never inserts what is already there, so you can paste any range you are unsure about:

- A message the inbox already has is skipped and counted: same id, or the same text within two minutes of the same send time (Telegram's copy rounds times, so 17:04:59 shows as 17:05:00). The same text on the same day at another time is held for `/review` instead, since it is probably the same message but might be a second identical purchase.
- A message that repeats a row already on the transactions tab, **same day, same wording, same amount and same currency**, is held as a duplicate: it is not queued, the reply lists it with the sheet row it repeats, and it waits in `/review`. Wording is compared without amounts, currency words, digits, symbols and case, and without the "Groceries - " prefix the bot adds, so "Migros 450 tl" and "Migros 450" both repeat a row "Groceries - Migros · ₺450" of the same day, while "Migros - Water 400 TL" and "Migros 400 TL" are two different purchases and both go through. A message listing several items (blank line between them) is held when any one of its items repeats a row.
- Everything else is queued as pending.

- A message with the **same sender and send time as a stored one but a different text** is that message, corrected: it is queued as a revision and the sync updates the rows it produced earlier, exactly as an edit in Telegram would. So if a copy came out wrong, fix the text in your paste and paste it again. (Telegram copies quick consecutive messages as one block; a block that spans several stored messages is skipped when it equals their texts and held for `/review` when it differs.)
- Lines that start with `...` are reported back: Telegram sometimes leaves a line out when several messages are copied at once (Persian text, typically) and shows `...` instead, so the amount below it has lost its description. Check those messages in the group and paste them again.

`/review` lists what is held; `/review keep 2` queues item 2 for the next sync anyway (it was not a duplicate after all), `/review done 2` (or `done all`) closes it. Nothing is dismissed by date unless you ask: `python bot.py backfill FILE --from 2026-07-29` drops everything before that day. Imported messages are processed 60 per Claude call.

The recognised paste format is what Telegram produces when you copy messages; a missing comma or colon, a 12-hour clock, and the Desktop variant (`Name, [01.09.2026 21:14]`) are all accepted:

```
Shiva, [1 Sep 2026 at 21:14:10]:
A101
2045

Hamed Shams, [3 Sep 2026 at 09:59:44]:
UBER to Metro Station
84 TL
```

## Configuration

All settings are environment variables. Defaults in **bold**.

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather. The only variable required to start. |
| `TELEGRAM_CHAT_ID` | Optional. The **group's** chat id (negative number); overrides what `/setup` stored. Every message anyone posts there is recorded. |
| `TELEGRAM_ADMIN_CHAT_ID` | Optional. Your private chat with the bot for full reports, which is your own Telegram user id; overrides `/setup`. Press Start in that chat once. |
| `ANTHROPIC_API_KEY` | Needed to sync. Until set, the bot records messages and reports the missing key. |
| `ANTHROPIC_MODEL` | **`claude-sonnet-5`** |
| `ANTHROPIC_EFFORT` | Thinking effort, `low`…`max`, used for every call including the retry. **`high`** |
| `ANTHROPIC_PRICE_INPUT_PER_MILLION`, `ANTHROPIC_PRICE_OUTPUT_PER_MILLION` | USD prices used to estimate cost in the run log. **2.0 / 10.0** |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The whole key file as one line (single-quoted in a `.env` file). Or `GOOGLE_SERVICE_ACCOUNT_FILE`, a path to the file, for local runs. Needed to store anything; the bot reports it when missing. |
| `GOOGLE_SHEET_ID` | Optional. The id from the spreadsheet URL (`/d/<id>/edit`). When empty, the bot finds the spreadsheet shared with the service account through the Google Drive API, which must then be enabled in the same Cloud project (the one containing `SHEET_TAB` wins if several are shared). |
| `SHEET_TAB` | Tab that receives transactions; created with a header row if missing. **`Transactions_Trip#2`** |
| `INBOX_TAB`, `RUNS_TAB`, `CONFIG_TAB` | Hidden tabs the bot creates: raw messages, run log, pairing. **`Bot_Inbox`, `Bot_Runs`, `Bot_Config`** |
| `SUMMARY_TAB` | Report tab built by `init-sheet`. **`Summary`** |
| `SYNC_CRON` | Crontab schedule in `TIMEZONE`. **`0 9 1,15 * *`** (09:00 on the 1st and 15th). Weekly Mondays: `0 9 * * 1` |
| `TIMEZONE` | **`Europe/Istanbul`** |
| `SCHEDULED_MIN_MESSAGES` | Minimum pending messages for a scheduled sync to call Claude. **5** |
| `MANUAL_MIN_MESSAGES` | Minimum pending messages for `/sync` to call Claude. **1** |
| `DAY_ROLLOVER_HOUR` | Messages before this hour count for the previous day. **4** |
| `DEFAULT_CURRENCY` | Used when no currency is written. **`TRY`** |
| `POST_SUMMARY` | Post a one-line summary in the group after each sync. **`true`** |
| `NOTE_KEYWORD` | Word that turns the rest of a message into a private note, never stored or sent to Claude. **`#note`** |
| `DECIMAL_SEPARATOR` | How the household writes numbers: `.` for 1,154.5 or `,` for 1.154,5. Plain 1154 works either way. **`.`** |
| `COLUMN_DATE`, `COLUMN_AMOUNT`, `COLUMN_CURRENCY`, `COLUMN_DESCRIPTION`, `COLUMN_CATEGORY` | Column letters on the transactions tab. Other columns are never touched. **B, C, D, E, G** |
| `PORT` | Set by Railway. The `/health` endpoint listens here. **8080** |

### Categories and currencies

Column G receives a category. The allowed names are read from **the sheet itself** at every sync: the bot looks at the dropdown on column G of the target tab and uses exactly those values, so the dropdown, your Summary formulas and the bot can never disagree. In Google's budget template that dropdown is fed from the category table in the Summary tab (`Summary!B28:B35`), so editing that table is all it takes to change categories. Placeholders such as "Custom category 1" are ignored; if the column has no dropdown, the same nine names below are the built-in fallback (`DEFAULT_CATEGORIES` in `extractor.py`). Short hints for common names live in `CATEGORY_HINTS` in the same file.

| Category | Covers |
|---|---|
| Groceries | supermarkets, markets, bakeries, water and other food for home |
| Eating Out | restaurants, cafes, coffee, bars, takeaway, delivery |
| Transport | taxi, Uber, Istanbulkart and public transport, fuel, parking |
| Housing & Utilities | rent, deposit, building fees, electricity, water, gas, the home internet (WiFi) bill, home supplies, furniture (an IKEA desk), repairs |
| Health & Personal Care | pharmacy, doctor, dentist, hospital, tests, insurance, barber, cosmetics, hygiene, gym |
| Shopping | clothes, shoes, electronics, gifts, malls and general retail not covered elsewhere |
| Leisure & Travel | going out and going away: cinema, concerts, events, hobbies, games, hotels, flights, tours, trips |
| Subscriptions | recurring paid services that are not a home utility: mobile data packages and SIM top-ups, app and software plans (Cursor, ChatGPT, Apple One), memberships such as Uber One |
| Other | fees, bank and government charges, documents, services, money transfers, anything that fits nowhere else |

The line between the last three matters: a recurring charge (a Turkcell data package, Cursor, Apple One) is a Subscription, the home WiFi bill is a utility, an Istanbulkart top-up is Transport, and Leisure & Travel never takes a recurring service. To add a category, type its name in the next free cell of the Summary tab's category list; to re-check the whole history against a changed list, run `python bot.py categorise --all --dry-run` and then without `--dry-run` (only the category cells that change are written).

Column D receives one of TRY, TOMAN, EUR, USD, GBP (`Currency` in `extractor.py`); Turkish, English, German and Persian currency words and Persian digits are understood. Wording rules, store names and examples live in `prompt.md`.

## Commands

In Telegram:

| Command | Where | What it does |
|---|---|---|
| `/setup` | in the group, by a group admin | Pairs the bot with that group and with you. Once. |
| `/sync` (or `@botname /sync`) | group or private chat | Processes everything pending now. In the group you get the one-line summary; in the private chat the full report. |
| `/backfill` | private chat | Start pasting older messages. The import starts 20 s after the last part, or at once on `/done`; `/cancel` discards. Members of the paired group only. |
| `/review` | group or private chat | What waits for a person: imports held as possible duplicates and messages Claude was unsure about, numbered. `/review keep 2` queues item 2 for the next sync anyway; `/review done 2` (or `done all`) closes it. |
| `/status` | anywhere | What is connected and what still needs setting up. |
| `/start`, `/help` | anywhere | Who the bot is talking to, the same status, and this list. |

From a terminal with a `.env` file (`pip install -r requirements.txt` first):

```bash
python bot.py check           # verifies Telegram, Sheets, Anthropic, pairing and the schedule; no paid call
python bot.py sync --dry-run  # calls Claude, prints the rows it would write, writes nothing
python bot.py sync            # one real sync from the terminal
python bot.py backfill FILE   # queue older messages from a paste (.txt) or a Telegram Desktop export (.json)
python bot.py init-sheet      # build the Summary report tab; --rewrite replaces an existing one, keeping its exchange rates
python bot.py categorise --rows 5:152 --dry-run   # propose categories for hand-entered rows that have none; drop --dry-run to write them
python bot.py categorise --all --dry-run          # re-check every row against the current category list; writes only what changes
python bot.py resync --since 2026-07-30           # re-extract messages that already have rows so the rows follow the current prompt rules
python bot.py                 # run the bot locally (stop it before deploying: two pollers on one token conflict)
```

## The Summary tab

`python bot.py init-sheet` builds a report tab over your transactions tab, made only of formulas so it stays live:

- spend, share and number of transactions per category, with a donut chart;
- spend per month, with a column chart;
- totals per currency, as logged and converted;
- an exchange-rate table, and the ten largest expenses.

Cell B3 on that tab is a dropdown of the supported currencies, prefilled from `DEFAULT_CURRENCY`. Every amount on the tab is converted into that currency, so picking another entry switches the whole report, charts included. The conversion uses the **Exchange rates** table lower on the tab: one editable cell per currency holding the value of one unit, all measured in the same currency of your choice (only the ratios matter). `init-sheet` prefills it with 1 for the default currency and Google Finance formulas for the others; the Iranian toman has no reliable feed and is entered by hand (an empty rate makes that currency count as 0 and the per-currency table says "rate missing"). You can replace any rate with a number or a reference to your own rates table. Rows whose amount is not a number (a note typed across a row) are ignored by every formula. The category names in column A of that tab are what the category dropdown on the transactions tab offers, so adding a category is typing it in the next free cell. An existing tab of that name is left alone unless you pass `--rewrite`, which deletes and rebuilds it but keeps the exchange rates you entered; the transactions tab is never touched. Starting from a blank spreadsheet, `init-sheet` plus the tab the bot creates by itself gives you the whole layout.

## Categorising rows you entered by hand

Rows that existed before the bot usually have no category, so the Summary shows them on one line instead of in the shares. `python bot.py categorise --rows 5:152 --dry-run` sends every row in that range that has a description but no category to Claude in one call (plain JSON at the configured effort, categories limited to the sheet's own list), prints the proposed category per row and the per-category counts, and writes nothing. Without `--dry-run` it writes those category cells and nothing else: each cell is re-checked to be still empty right before the write, rows without a description (a note typed across a row, a blank line) are never touched, and every other column stays as it is. Rows Claude does not answer are named so you can run it again.

## Money coming back: the `++` trick

Not every line in the group is money leaving. Put `++` in front of an amount and the bot files it as money that came back, stored as a **negative** row, so every total on the Summary tab subtracts it by itself. Two everyday uses:

```
💶 💰 PROFIT from Time-Deposit Investment
++ 3,780 TL
```
The bank paid interest? It shrinks this month's spending instead of vanishing into a note.

```
💸💰 Lamp cancelled and this amount refunded
++ 971 TL
```
Something cancelled and refunded? The refund lands in the category of what was bought, and the month is square again.

No `++`, no negative number: money that merely arrives (a salary, a friend paying you back) is still flagged for your decision rather than filed.

## Descriptions stay yours

What you write is what lands in the description column: parentheses, remarks, names and emojis included ("Trendyol 🛒 (incl 🖥️ monitor)" stays exactly that; a lone "🍆" becomes "Eggplant 🍆"). A two-line description is joined into one, words after the amount are appended ("290 euro (1€=200t)" keeps its note), a little arithmetic under an item ("2192-1200 / = 992 TL") is one row of 992 with the arithmetic kept in the text, and grocery stores get a "Groceries - " prefix. When the prompt rules change, `python bot.py resync` re-extracts messages that already have rows and updates those rows in place.

## Private notes

Sometimes a message is for the two of you, not for the sheet. Put `#note` in front of that part and the bot drops it before anything is stored:

```
Gratis 266 TL #note birthday present, don't tell
→ recorded as “Gratis”, 266 TRY. The note is gone; it never reached the sheet or Claude.

#note let's review the budget on Friday
→ nothing recorded; the inbox shows a “[note]” line so you can see it was seen and skipped.
```

The keyword is matched as a whole word, in any case, anywhere in the message, and everything after it is dropped. Editing a pending message to start with `#note` retires it. The same rule applies to imported history. Change the word with `NOTE_KEYWORD`.

## Privacy

What leaves your Telegram group, and where it goes:

- The text of messages in the paired group (captions included) is stored in the hidden `Bot_Inbox` tab of your own spreadsheet as it arrives, and sent to Anthropic's API at sync time so Claude can turn it into rows. Nothing else is sent: no photos, voice messages or files, no member names beyond the sender's first name, no messages from other chats.
- Text after the note keyword (`#note` by default) is dropped before storage and never sent anywhere.
- Anthropic's API terms and data policy apply to what is sent; check their current documentation for retention and training rules.
- The service account can only reach spreadsheets you share with it. The bot keeps no other copy of your data: no database, no files, and its log lines carry message ids and counts, not message text.

## Security

- Only the paired group is recorded. Messages in any other chat are ignored, and while paired the bot leaves any other group it is added to.
- `/sync` and `/backfill` work only for members of the paired group (checked with Telegram); `/setup` only for a group admin, and only while the bot is unpaired or from the fixed group. Strangers who message the bot privately get a one-line refusal and learn nothing about your setup.
- For a personal instance, also tell BotFather `/setjoingroups` → Disable, so nobody can add your bot to another group at all, and rotate the token with `/revoke` if it ever leaks. Secrets live in environment variables only, never in the repository.

## Guardrails

- New rows only ever go below the last used row, after the bot verifies that the destination cells are empty. Rows it wrote earlier are updated or removed only through their provenance note, when their Telegram message is edited or deleted, and a row is re-checked immediately before deletion. Columns it is not configured for are never touched.
- Descriptions are written formula-safe: a leading `=`, `+`, `-` or `@` is neutralised.
- Column C gets the number format of its own currency (₺, €, $, £, or a TOMAN suffix), so the symbol always matches column D.
- Rows the bot wrote carry a provenance note; only rows with the right note are ever updated or removed, and a row is re-checked immediately before deletion.
- Only one sync runs at a time, and a message is inserted at most once (status column plus de-duplication by id); imports also check the sheet itself for a matching row.
- `Bot_Inbox` is a log the bot only appends to: every change of a message's status is a new row, nothing there is ever updated or deleted, and the newest row of a message is its current state. Outside the transactions tab the bot writes only to its own hidden tabs (`Bot_Inbox`, `Bot_Runs`, `Bot_Config`). Share the spreadsheet with the service account and nothing else.
- Every failure is reported in words: a missing variable names the variable, an unreadable key file says how to fix it, a spreadsheet that cannot be opened says which e-mail address to share it with, and a sync that fails posts the error to you while the messages stay pending.

## When Claude or Telegram fail

Calls to Anthropic are retried by the SDK on rate limits, server errors and connection problems: up to 3 retries with exponential backoff starting at half a second and capped at 8 seconds, honouring any `retry-after` the API sends. A request that still fails leaves every message pending and the failure is reported to you; the next sync retries. On top of that, an answer that comes back damaged, invalid or cut short triggers one more call at the same effort (see above). Telegram delivery failures are logged and never break a sync.

## Health and monitoring

`GET /health` on `$PORT` answers `200` with a small JSON (`status`, `uptime_seconds`, `paired`, `syncing`, `spreadsheet`). `railway.json` points Railway's healthcheck at it. Every sync also leaves a row in `Bot_Runs` and a report in your private chat.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline and cover configuration validation, prompt rendering and the exact output schema, date rollover, message pairing and inbox statuses, the append-only inbox log, report texts, the paste and JSON parsers, the import's duplicate checks, `/review`, and the append-only guardrail. GitHub Actions runs them on every push.

### Reading the inbox tab

The tab is a log. The bot never edits or deletes a row there: storing a message, marking it after a sync, recording an edit, noticing a deletion, holding an import or closing an item each append a new row for the same message id, with `logged_at` set. The newest row of a message is its current state; the rows above it are its history.

| Status | Meaning |
|---|---|
| `pending` | Stored, not yet processed. |
| `processed` | One or more rows were written for it (`rows_added`). |
| `merged` | Folded into another message's transaction, for example an amount sent as a separate message, or a correction. |
| `skipped` | Not an expense (chit-chat, a recap, a test message). |
| `needs_review` | Claude was not sure about something in it; the note says what. Rows it was sure about are still written. |
| `pending_revision` | The message was edited after its rows were written; the next sync updates or removes those rows, unless the new answer looks incomplete, in which case the rows stay and the message is flagged. |
| `pending_deletion`, `deleted` | The message was deleted in Telegram after its rows were written; the next sync removes the rows and the history stays, with the text, for tracing. |
| `duplicate` | An imported message that repeats a row already on the sheet (same day, wording, amount and currency). Not queued; waits in `/review`. |
| `resolved` | Closed by a person with `/review done`. |

## Roadmap

Ideas that are not built yet. Open an issue if you want one sooner, or have another.

- **Voice messages.** Say the expense out loud in the group; the bot transcribes the voice note and files it like text. Today voice notes are acknowledged and never processed.

## Cost

With Claude Sonnet 5 at $2 per million input tokens and $10 per million output tokens, a household logging about 150 expenses a month costs roughly **$0.10–0.25 a month** in API usage at two syncs a month, and about the same at four. The prompt and schema are about 4,500 tokens per call; output grows with the number of expenses, not with the number of syncs. Measured: ten messages in one scheduled run cost $0.037 at effort `high`; the same work at `low` is cheaper still. A retry after a damaged answer adds one call. Each run's token counts, calls and estimated cost are written to `Bot_Runs`.

## Setting up piece by piece

Only `TELEGRAM_BOT_TOKEN` is needed to start the bot. Add the rest in any order and ask it `/status`: it lists each integration with ✅ or ❌ and, for a ❌, the exact fix (which e-mail address to share the spreadsheet with, which variable to set, which command to type). It retries a missing integration every minute, so nothing needs a redeploy once fixed. `python bot.py check` prints the same checklist in the terminal.

## Limits worth knowing

- Bots cannot read chat history: messages sent before the bot joined are not seen. Use `backfill` with a Telegram Desktop export for those.
- Telegram does not notify bots about deleted messages. To retract a note before a sync, edit it to say "ignore" or "cancelled".
- Edits follow through, carefully. Editing a pending message replaces its text. Editing a message that was skipped or flagged makes it pending again. Editing a message that already produced sheet rows re-syncs it at the next sync: its rows are updated in place, extra rows removed, missing rows added. Editing it into `#note` (or “cancelled”) removes its rows. The bot finds its own rows through a small note it leaves on each date cell (“kashio:<message id>”), so it never touches rows it did not write. If the new answer looks incomplete (Claude unsure, or far fewer items than before or than the text lists), the rows are left exactly as they were and you are told; edit the message again to retry.
- Deletions follow through too. Telegram sends bots no event for a deleted message, so before each sync the bot asks Telegram to clear its own (non-existent) reaction on every message that has rows in the sheet; a deleted message answers "not found", and its rows are removed at that sync. Existing messages are unaffected whether or not people have reacted to them (a bot can only change its own reaction, so theirs are never touched), nothing visible happens in the group, only recent messages are probed (the last 45 days on scheduled syncs, the last week on `/sync`, one probe every three seconds because Telegram allows about twenty calls a minute per group), and the inbox keeps the message's text and history with the status `deleted` so you can trace it later. Deletions and note-edits are applied even when the scheduled minimum is not met, since they cost no Claude call.
- If Telegram upgrades your group to a supergroup, its id changes; update `TELEGRAM_CHAT_ID`.
- Two people writing a few notes a day stay far below Google Sheets API quotas.


## Author and license

Made with ❤️ & ☕ by **Hamed Shams** · [www.HamedShams.com](https://www.HamedShams.com)

Released under the [MIT License](LICENSE).
