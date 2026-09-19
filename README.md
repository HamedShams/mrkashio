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
- **Every answer is checked.** Claude's reply is audited (a result for a message that was never sent, an unreadable description, a malformed date, a missing message); if anything is off, the batch is asked once more without extended thinking, and whatever still has no usable answer stays pending and is named in the report. Nothing invented ever reaches the sheet.
- **It only ever appends.** The bot writes below the last used row, checks that the destination cells are empty a moment before writing, and never edits or deletes an existing row of your transactions tab. See Guardrails.
- **It tells you what is missing.** The bot starts with nothing but a Telegram token. Whatever else is absent or broken (the Google key, a spreadsheet that is not shared, the Anthropic key, the group pairing) is reported in plain words to whoever talks to it, with the fix, by `/status`, `/start` and any command that cannot run. No model is involved in that: plain checks and prewritten sentences.
- **Private notes stay private.** Anything from the word `#note` to the end of a message is a note for the humans: it is never stored, never sent to Claude, and a message that is only a note leaves nothing but a “[note]” acknowledgement in the inbox. The word is configurable (`NOTE_KEYWORD`).
- **Photos, voice messages and files are never touched.** Only text is read. A photo without a caption is acknowledged in the inbox as “[photo]”, skipped, and never downloaded, uploaded or sent to Claude. A caption is treated as text.

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
2. APIs & Services → Library → **Google Sheets API** → Enable. Enable the **Google Drive API** too if you want the bot to find your spreadsheet by itself instead of you pasting its id.
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

- **Paste them to the bot.** In a private chat with the bot, send `/backfill`, then paste the messages copied from the Telegram chat (select messages → Copy). Telegram splits a long paste into several messages by itself, so the bot cannot know when the last part has arrived: it waits 20 seconds after the last part, or starts at once when you send `/done`. `/cancel` discards. You can also put the paste right after `/backfill` in one message. The bot queues the messages and runs a sync immediately.
- **From a file.** `python bot.py backfill result.json` for a Telegram Desktop export (chat menu → Export chat history → JSON), or a `.txt` with the copied messages. Then `python bot.py sync --dry-run` and `python bot.py sync`.

Both skip everything dated on or before the sheet's last recorded date (those rows were entered by hand already) and anything already stored, so repeating an import is harmless. The reply lists every dismissed message with its date and text, so nothing disappears silently. `--from 2026-07-29` starts from an earlier day if the last recorded day was only partly entered. Imported messages are processed 150 per Claude call.

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
| `ANTHROPIC_EFFORT` | Thinking effort, `low`…`max`. **`low`**. Higher settings made long batches come back damaged in testing; the audit and retry cover that, but `low` is both cheaper and more reliable here. |
| `ANTHROPIC_PRICE_INPUT_PER_MILLION`, `ANTHROPIC_PRICE_OUTPUT_PER_MILLION` | USD prices used to estimate cost in the run log. **2.0 / 10.0** |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The whole key file as one line (single-quoted in a `.env` file). Or `GOOGLE_SERVICE_ACCOUNT_FILE`, a path to the file, for local runs. Needed to store anything; the bot reports it when missing. |
| `GOOGLE_SHEET_ID` | Optional. From the spreadsheet URL. When empty, the bot finds the spreadsheet shared with the service account through the Drive API (the one containing `SHEET_TAB` if several are shared). |
| `SHEET_TAB` | Tab that receives transactions; created with a header row if missing. **`Transactions_Trip#2`** |
| `INBOX_TAB`, `RUNS_TAB`, `CONFIG_TAB` | Hidden tabs the bot creates: raw messages, run log, pairing. **`Bot_Inbox`, `Bot_Runs`, `Bot_Config`** |
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

Column G receives a category. The allowed names are read from **the sheet itself** at every sync: the bot looks at the dropdown on column G of the target tab and uses exactly those values, so the dropdown, your Summary formulas and the bot can never disagree. In Google's budget template that dropdown is fed from the category table in the Summary tab (`Summary!B28:B35`), so editing that table is all it takes to change categories. Placeholders such as "Custom category 1" are ignored; if the column has no dropdown, the same eight names below are the built-in fallback (`DEFAULT_CATEGORIES` in `extractor.py`). Short hints for common names live in `CATEGORY_HINTS` in the same file.

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

Column D receives one of TRY, TOMAN, EUR, USD, GBP (`Currency` in `extractor.py`); Turkish, English, German and Persian currency words and Persian digits are understood. Wording rules, store names and examples live in `prompt.md`.

## Commands

In Telegram:

| Command | Where | What it does |
|---|---|---|
| `/setup` | in the group, by a group admin | Pairs the bot with that group and with you. Once. |
| `/sync` (or `@botname /sync`) | group or private chat | Processes everything pending now. In the group you get the one-line summary; in the private chat the full report. |
| `/backfill` | private chat | Start pasting older messages. The import starts 20 s after the last part, or at once on `/done`; `/cancel` discards. Members of the paired group only. |
| `/status` | anywhere | What is connected and what still needs setting up. |
| `/start`, `/help` | anywhere | Who the bot is talking to, the same status, and this list. |

From a terminal with a `.env` file (`pip install -r requirements.txt` first):

```bash
python bot.py check           # verifies Telegram, Sheets, Anthropic, pairing and the schedule; no paid call
python bot.py sync --dry-run  # calls Claude, prints the rows it would write, writes nothing
python bot.py sync            # one real sync from the terminal
python bot.py backfill FILE   # queue older messages from a paste (.txt) or a Telegram Desktop export (.json)
python bot.py                 # run the bot locally (stop it before deploying: two pollers on one token conflict)
```

## Private notes

Sometimes a message is for the two of you, not for the sheet. Put `#note` in front of that part and the bot drops it before anything is stored:

```
Gratis 266 TL #note birthday present, don't tell
→ recorded as “Gratis”, 266 TRY. The note is gone; it never reached the sheet or Claude.

#note let's review the budget on Friday
→ nothing recorded; the inbox shows a “[note]” line so you can see it was seen and skipped.
```

The keyword is matched as a whole word, in any case, anywhere in the message, and everything after it is dropped. Editing a pending message to start with `#note` retires it. The same rule applies to imported history. Change the word with `NOTE_KEYWORD`.

## Guardrails

- The bot only appends to the transactions tab. It finds the last row that holds a date, amount or description, verifies that the destination cells below it are empty immediately before writing, and refuses to write otherwise. It never issues an edit or delete against an existing row, and it never touches columns A and F.
- Descriptions are written formula-safe: a leading `=`, `+`, `-` or `@` is neutralised.
- Column C gets the number format of its own currency (₺, €, $, £, or a TOMAN suffix), so the symbol always matches column D.
- Rows the bot wrote carry a provenance note; only rows with the right note are ever updated or removed, and a row is re-checked immediately before deletion.
- Only one sync runs at a time, and a message is inserted at most once (status column plus de-duplication by id).
- The bot updates cells only in its own hidden tabs (`Bot_Inbox`, `Bot_Runs`, `Bot_Config`). Share the spreadsheet with the service account and nothing else.
- Every failure is reported in words: a missing variable names the variable, an unreadable key file says how to fix it, a spreadsheet that cannot be opened says which e-mail address to share it with, and a sync that fails posts the error to you while the messages stay pending.

## Health and monitoring

`GET /health` on `$PORT` answers `200` with a small JSON (`status`, `uptime_seconds`, `paired`, `syncing`, `spreadsheet`). `railway.json` points Railway's healthcheck at it. Every sync also leaves a row in `Bot_Runs` and a report in your private chat.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run offline and cover configuration validation, prompt rendering and the exact output schema, date rollover and the cutoff rule, message pairing and inbox statuses, report texts, the paste and JSON parsers, the import filters, and the append-only guardrail. GitHub Actions runs them on every push.

### Reading the inbox tab

| Status | Meaning |
|---|---|
| `pending` | Stored, not yet processed. |
| `processed` | One or more rows were written for it (`rows_added`). |
| `merged` | Folded into another message's transaction, for example an amount sent as a separate message, or a correction. |
| `skipped` | Not an expense (chit-chat, a recap, a test message). |
| `needs_review` | Claude was not sure about something in it; the note says what. Rows it was sure about are still written. |
| `pending_revision` | The message was edited after its rows were written; the next sync updates or removes those rows. |

## Cost

With Claude Sonnet 5 at $2 per million input tokens and $10 per million output tokens, a household logging about 150 expenses a month costs roughly **$0.10–0.25 a month** in API usage at two syncs a month, and about the same at four. The prompt and schema are about 4,500 tokens per call; output grows with the number of expenses, not with the number of syncs. Measured: ten messages in one scheduled run cost $0.037 at effort `high`; the same work at `low` is cheaper still. A retry after a damaged answer adds one call. Each run's token counts, calls and estimated cost are written to `Bot_Runs`.

## Setting up piece by piece

Only `TELEGRAM_BOT_TOKEN` is needed to start the bot. Add the rest in any order and ask it `/status`: it lists each integration with ✅ or ❌ and, for a ❌, the exact fix (which e-mail address to share the spreadsheet with, which variable to set, which command to type). It retries a missing integration every minute, so nothing needs a redeploy once fixed. `python bot.py check` prints the same checklist in the terminal.

## Limits worth knowing

- Bots cannot read chat history: messages sent before the bot joined are not seen. Use `backfill` with a Telegram Desktop export for those.
- Telegram does not notify bots about deleted messages. To retract a note before a sync, edit it to say "ignore" or "cancelled".
- Edits follow through. Editing a pending message replaces its text. Editing a message that was skipped or flagged makes it pending again. Editing a message that already produced sheet rows re-syncs it at the next sync: its rows are updated in place, extra rows removed, missing rows added. Editing it into `#note` (or “cancelled”) removes its rows. The bot finds its own rows through a small note it leaves on each date cell (“kashio:<message id>”), so it never touches rows it did not write. Telegram does not tell bots about deleted messages, so to remove an expense, edit the message rather than delete it.
- If Telegram upgrades your group to a supergroup, its id changes; update `TELEGRAM_CHAT_ID`.
- Two people writing a few notes a day stay far below Google Sheets API quotas.

## Author and license

Made by **Hamed Shams** · [www.HamedShams.com](https://www.HamedShams.com)

Released under the [MIT License](LICENSE).
