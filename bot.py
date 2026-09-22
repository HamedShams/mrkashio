"""Kashio: Telegram entry point, scheduler and command-line tools.

    python bot.py                 start the bot (what Railway runs)
    python bot.py check           verify configuration and access; makes no paid call
    python bot.py sync            run one sync from the terminal and exit
    python bot.py sync --dry-run  call Claude and print the rows, but write nothing and message nobody
    python bot.py backfill FILE   queue older messages from a pasted dump or a Telegram Desktop JSON export
    python bot.py init-sheet      build the Summary report tab (add --rewrite to replace an existing one)
    python bot.py categorise      fill in the empty category cells of hand-entered rows (--rows 5:152, --dry-run);
                                  --all re-checks every row and rewrites only the categories that change
    python bot.py resync          re-extract messages that already have rows, so the rows follow the current prompt
                                  rules (--since 2026-07-30, --dry-run); rows are updated in place

Telegram commands:
    /setup     in the group, once, by a group admin: pairs the bot with that group and with you
    /sync      process everything pending now (also accepted as "@botname /sync")
    /backfill  in a private chat with the bot: paste copied messages; the import starts by itself after a short
               pause, or immediately on /done (/cancel discards)
    /review    what waits for a person: imports held as possible duplicates and Claude's doubts;
               /review keep <n> queues an item for the next sync anyway, /review done <n> (or all) closes it
    /report    spending by month in the default currency, or in the one named (/report €, /report usd, /report lira);
               computed from the sheet with the Summary tab's exchange rates, no model involved
    /status    what is connected and what still needs setting up
    /start     who am I talking to, and the commands

Mentioning the bot in the group (@botname, alone or before a command) gets the same answer as /start, or runs the
command named after the mention. A mention followed by something that has a number in it is treated as an expense
note and stored like any other message.

Deleted messages: Telegram sends bots no event for a deletion, so before each sync the bot asks Telegram to
clear its (non-existent) reaction on every message that has rows in the sheet; a deleted message answers
"not found" and its rows are removed at that sync. No visible side effect on existing messages, and the
reactions people put on messages are never touched (a bot can only change its own).

The inbox tab is a log: the bot appends a row for every change of a message's status and never updates
or deletes a row there; the newest row of a message is its current state.

The bot starts with nothing but a Telegram token. Anything else that is missing or broken (Google Sheets,
the Anthropic key, the group pairing) is reported in plain words to whoever talks to it, with the fix.
Only the paired group is ever recorded. If the bot is added to another group while paired, it leaves it.
A tiny HTTP endpoint answers GET /health on $PORT for Railway's healthcheck.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
from apscheduler.triggers.cron import CronTrigger
from gspread.exceptions import APIError
from telegram import Bot, Chat, Message, MessageEntity, Update, User
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from backfill import import_messages, parse
from categorise import apply as apply_categories, candidates as category_candidates, plan as plan_categories
from summary import build_summary, format_monthly_report, monthly_totals
from config import ConfigError, Settings
from extractor import MAX_MESSAGES_PER_CALL, clean_categories, load_prompt, parse_currency
from sheets import STATUS_PENDING, STATUS_PENDING_REVISION, STATUS_RESOLVED, STATUS_SKIPPED, InboxMessage, SheetStore
from sync import (
    STATUS_FAILED,
    STATUS_OK,
    TRIGGER_CLI,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    RunReport,
    format_report,
    format_summary,
    run_sync,
    split_note,
)

log = logging.getLogger("kashio")

TELEGRAM_MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message
BACKFILL_QUIET_SECONDS = 20  # a paste split into several messages is imported once nothing new arrives for this long
LONG_PASTE_CHARS = 3500  # Telegram cuts messages at 4096 characters: a /backfill this long is probably only the first part
RECONNECT_EVERY_SECONDS = 60  # how often a missing integration is retried
ANTHROPIC_MAX_RETRIES = 3  # SDK retries on 408/409/429/5xx and connection errors with exponential backoff (0.5 s → 8 s)
DELETION_PROBE_PAUSE = 3.1  # seconds between probes: Telegram allows about 20 calls a minute per group (hit on 22 Sep 2026)
DELETION_PROBE_DAYS = 45  # scheduled and terminal syncs probe messages this recent; deletions of older notes are rare
MANUAL_PROBE_DAYS = 7  # /sync probes only the last week, so the person typing it is not kept waiting
HINT_EVERY_SECONDS = 3600  # how often the group is reminded that something is not set up
GROUP_ROLES = ("member", "administrator", "creator")
ADMIN_ROLES = ("administrator", "creator")
HELP = (
    "Commands:\n"
    "/sync — process everything pending now\n"
    "/backfill — paste older messages (private chat); import starts after a short pause or on /done\n"
    "/review — what waits for a look: imports held as possible duplicates, Claude's doubts; "
    "/review keep <n> queues one anyway, /review done <n> closes it\n"
    "/report — spending by month in the default currency; /report €, /report usd, /report lira for another\n"
    "/setup — in the group, once, to pair me with it\n"
    "/status — what is connected and what is still missing\n"
    "/start — this message"
)
NOTE_REASON = "private note ({keyword}); its content was not stored and not sent to Claude"
MEDIA_REASON = "{kind}: not an expense note; the file was not downloaded, uploaded or sent to Claude"
STRANGER_REPLY = "Hi! I'm Kashio, a private bot that files one household's expense notes. I can't help you here."


class Kashio:
    """Long-lived clients, the pairing configuration, and the rule that only one sync runs at a time.

    Integrations are optional at startup: whatever cannot be reached is remembered as a plain-language
    problem, retried now and then, and shown to whoever talks to the bot.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prompt = load_prompt(settings)
        self.lock = asyncio.Lock()
        self.store: SheetStore | None = None
        self.store_error: str | None = None
        self.claude: anthropic.Anthropic | None = None
        self.claude_error: str | None = None
        self._config: dict[str, str] = {}
        self._last_connect = 0.0
        self._last_hint: dict[int, float] = {}
        self.connect(force=True)

    # -- integrations
    def connect(self, force: bool = False) -> None:
        """(Re)connect whatever is missing, at most once a minute unless forced."""
        now = time.monotonic()
        if not force and now - self._last_connect < RECONNECT_EVERY_SECONDS:
            return
        self._last_connect = now
        if self.store is None:
            try:
                self.store = SheetStore(self.settings)
                self.store_error = None
                self.reload_config()
                log.info("Google Sheets connected: %r, tab %r", self.store.spreadsheet.title, self.settings.sheet_tab)
            except ConfigError as exc:
                self.store_error = str(exc)
            except APIError as exc:
                self.store_error = (f"Google Sheets answered {exc}. Check that the spreadsheet is shared with the "
                                    f"service account as Editor and that the Google Sheets API is enabled.")
            except Exception as exc:  # noqa: BLE001 - anything else is still worth explaining rather than crashing
                self.store_error = f"Google Sheets could not be reached ({type(exc).__name__}: {exc})."
            if self.store_error:
                log.warning("Google Sheets not available: %s", self.store_error)
        if self.claude is None:
            self.claude_error = self._connect_claude()
            if self.claude_error:
                log.warning("Claude not available: %s", self.claude_error)

    def _connect_claude(self) -> str | None:
        if not self.settings.anthropic_api_key:
            return "ANTHROPIC_API_KEY is not set. Create a key at console.anthropic.com and add it to the environment."
        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key, max_retries=ANTHROPIC_MAX_RETRIES)
        try:
            client.models.retrieve(self.settings.anthropic_model)  # free call; proves the key and the model name
        except anthropic.AuthenticationError:
            return "ANTHROPIC_API_KEY was rejected by Anthropic. Check the key in the environment."
        except anthropic.PermissionDeniedError:
            return f"The Anthropic key is not allowed to use {self.settings.anthropic_model!r}. Check the key's workspace."
        except anthropic.NotFoundError:
            return f"Anthropic has no model called {self.settings.anthropic_model!r}. Check ANTHROPIC_MODEL."
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            log.warning("Anthropic could not be verified right now (%s); assuming it works", type(exc).__name__)
        self.claude = client
        return None

    @property
    def can_sync(self) -> bool:
        return self.store is not None and self.claude is not None

    @property
    def ready(self) -> bool:
        return self.can_sync and self.group_id is not None

    def status_lines(self, bot_username: str | None = None) -> list[str]:
        """A checklist anyone can act on. No model involved: plain validations and prewritten sentences."""
        lines = [f"✅ Telegram: connected as @{bot_username}" if bot_username else "✅ Telegram: connected"]
        if self.store is not None:
            lines.append(f"✅ Google Sheets: “{self.store.spreadsheet.title}”, tab “{self.settings.sheet_tab}”")
        else:
            lines.append(f"❌ Google Sheets: {self.store_error}")
        if self.claude is not None:
            lines.append(f"✅ Claude: {self.settings.anthropic_model} ready (effort {self.settings.anthropic_effort})")
        else:
            lines.append(f"❌ Claude: {self.claude_error}")
        if self.group_id is not None:
            title = self._config.get("group_title") or str(self.group_id)
            lines.append(f"✅ Paired with group “{title}”")
        else:
            lines.append("⚠️ Not paired with a group yet: add me to your group and, as a group admin, type /setup there.")
        if self.admin_id is not None:
            lines.append("✅ Full reports go to the admin's private chat")
        else:
            lines.append("ℹ️ Full reports go to the group (no admin chat set; /setup sets one)")
        return lines

    def status_text(self, bot_username: str | None = None, as_html: bool = False) -> str:
        head = "All set, I'm working." if self.ready else "🛠 Not quite ready yet. Here is what I can see:"
        lines = self.status_lines(bot_username)
        if as_html:
            return f"<b>{html.escape(head, quote=False)}</b>\n" + "\n".join("• " + html.escape(line, quote=False) for line in lines)
        return head + "\n" + "\n".join(lines)

    def hint_due(self, chat_id: int) -> bool:
        """True at most once an hour per chat, so a broken setup does not flood the group."""
        now = time.monotonic()
        if now - self._last_hint.get(chat_id, -HINT_EVERY_SECONDS) < HINT_EVERY_SECONDS:
            return False
        self._last_hint[chat_id] = now
        return True

    # -- pairing: environment variables win, otherwise what /setup stored in the sheet
    def reload_config(self) -> None:
        self._config = self.store.read_config() if self.store else {}

    @property
    def group_id(self) -> int | None:
        return self.settings.telegram_chat_id or _as_int(self._config.get("group_id"))

    @property
    def admin_id(self) -> int | None:
        return self.settings.telegram_admin_chat_id or _as_int(self._config.get("admin_id"))

    def source_of(self, key: str) -> str:
        env = self.settings.telegram_chat_id if key == "group_id" else self.settings.telegram_admin_chat_id
        if env is not None:
            return "environment variable"
        return "sheet (/setup)" if self._config.get(key) else "not set"

    # -- syncing
    async def sync(self, trigger: str, requested_by: str, dry_run: bool = False) -> RunReport | None:
        """Run a sync in a worker thread. Returns None if another sync is already running."""
        assert self.store is not None and self.claude is not None, "sync called while not ready"
        if self.lock.locked():
            return None
        async with self.lock:
            return await asyncio.to_thread(
                run_sync, self.settings, self.store, self.claude, self.prompt,
                trigger=trigger, requested_by=requested_by, dry_run=dry_run,
            )

    async def notify(self, bot: Bot, report: RunReport, already_informed: frozenset[int] = frozenset()) -> None:
        """Full report to the admin chat (or the group if none is set); short summary to the group."""
        group = self.group_id
        admin = self.admin_id or group
        if admin is not None and admin not in already_informed:
            await deliver(bot, admin, format_report(report, as_html=True), as_html=True)
        if (
            self.settings.post_summary
            and group is not None
            and group != admin
            and group not in already_informed
            and report.status in (STATUS_OK, STATUS_FAILED)
        ):
            await deliver(bot, group, format_summary(report, as_html=True), as_html=True)

    async def detect_deletions(self, bot: Bot, days: int = DELETION_PROBE_DAYS) -> list[int]:
        """Find messages that were deleted in Telegram after their rows were written, and queue their rows for removal.

        Telegram sends no event for deletions. Clearing the bot's reaction on a message is a harmless probe:
        an existing message answers "Reaction_empty", a deleted one "Message to react not found".
        """
        if self.store is None or self.group_id is None:
            return []
        horizon = datetime.now(self.settings.timezone) - timedelta(days=days)
        candidates = [m for m in await asyncio.to_thread(self.store.messages_with_rows) if m.sent_at >= horizon]
        deleted: list[InboxMessage] = []
        unknown = 0
        for message in candidates:
            try:
                try:
                    await bot.set_message_reaction(self.group_id, message.message_id, reaction=[])  # only the bot's own reaction
                except RetryAfter as exc:  # Telegram asks for a pause: wait it out and probe this message once more
                    pause = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
                    log.warning("Deletion check: Telegram asks for a %.0f s pause", pause)
                    await asyncio.sleep(pause + 1)
                    await bot.set_message_reaction(self.group_id, message.message_id, reaction=[])
            except BadRequest as exc:
                text = str(exc).lower()
                if "chat not found" in text:
                    log.warning("Deletion check aborted: Telegram cannot find the group (%s)", exc)
                    return []
                if "message to react not found" in text or "message not found" in text:
                    deleted.append(message)
                elif "reaction_empty" not in text and "reaction is invalid" not in text:
                    unknown += 1
                    log.warning("Deletion check: unexpected answer for message %s: %s", message.message_id, exc)
            except TelegramError as exc:
                unknown += 1
                log.warning("Deletion check: could not probe message %s: %s", message.message_id, exc)
            await asyncio.sleep(DELETION_PROBE_PAUSE)
        if deleted and len(deleted) == len(candidates) and len(candidates) >= 3:
            log.warning("Deletion check: every one of %d messages looks deleted; that cannot be right, ignoring", len(candidates))
            return []
        if deleted:
            await asyncio.to_thread(self.store.mark_deleted, deleted)
            log.info("Deleted in Telegram: message ids %s", [m.message_id for m in deleted])
        return [m.message_id for m in deleted]

    async def is_household_member(self, bot: Bot, user: User | None) -> bool:
        """The admin, or anyone who is a member of the paired group."""
        if user is None:
            return False
        if user.id == self.admin_id:
            return True
        if self.group_id is None:
            return False
        try:
            member = await bot.get_chat_member(self.group_id, user.id)
        except TelegramError:
            return False
        return member.status in GROUP_ROLES


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


async def deliver(bot: Bot, chat_id: int, text: str, as_html: bool = False) -> bool:
    """Send a message without letting a Telegram failure break the sync that produced it.

    If Telegram rejects the HTML (an unescaped "<" somewhere), the same text goes out as plain text with the
    tags stripped rather than not at all.
    """
    try:
        while text:
            cut = TELEGRAM_MESSAGE_LIMIT
            if as_html and len(text) > cut:  # never split inside a tag or an entity
                cut = max(text.rfind("\n", 0, cut), 1)
            chunk, text = text[:cut], text[cut:]
            try:
                await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML if as_html else None)
            except BadRequest as exc:
                if not as_html or "parse" not in str(exc).lower():
                    raise
                log.warning("Telegram rejected the HTML (%s); sending it as plain text", exc)
                await bot.send_message(chat_id, html.unescape(re.sub(r"</?b>", "", chunk)))
        return True
    except TelegramError as exc:
        hint = " Open a private chat with the bot and press Start first." if "initiate" in str(exc) or "not found" in str(exc).lower() else ""
        log.error("Could not deliver a message to chat %s: %s.%s", chat_id, exc, hint)
        return False


def kashio_of(context: ContextTypes.DEFAULT_TYPE) -> Kashio:
    return context.application.bot_data["kashio"]


def _name(user: User | None) -> str:
    return user.first_name if user else "unknown"


def describe_media(message: Message) -> str:
    """What kind of non-text message this is. Only the kind is ever recorded; the file is never touched."""
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.voice:
        return "voice message"
    if message.audio:
        return "audio"
    if message.animation:
        return "GIF"
    if message.document:
        return f"file {message.document.file_name}" if message.document.file_name else "file"
    if message.sticker:
        return "sticker"
    if message.video_note:
        return "video note"
    if message.location:
        return "location"
    if message.contact:
        return "contact"
    if message.poll:
        return "poll"
    return "attachment"


# ---------------------------------------------------------------- group: ingest


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None:
        return
    app.connect()
    if app.group_id is None:
        log.info("Not paired yet. Message seen in group %s (%r): a group admin can type /setup there.", chat.id, chat.title)
        if app.hint_due(chat.id):
            await message.reply_text("Hi! I'm not paired with a group yet, so I'm not recording anything. "
                                     "A group admin can type /setup here to pair me with this group.")
        return
    if chat.id != app.group_id:
        return
    text = message.text or message.caption
    if not text:
        return
    text, had_note = split_note(text, app.settings.note_keyword)
    if app.store is None:
        log.warning("Message %s not stored: %s", message.message_id, app.store_error)
        if app.hint_due(chat.id):
            await message.reply_text("⚠️ I can't reach the spreadsheet, so I'm not recording messages right now.\n"
                                     f"{app.store_error}\nOnce fixed, edit a message (even slightly) and I'll pick it up.")
        return
    sender, sent_at = _name(message.from_user), message.date.astimezone(app.settings.timezone)
    attachment = f"with a {describe_media(message)} (the file was not downloaded, uploaded or sent to Claude)" if message.caption and not message.text else ""
    try:
        if had_note and not text.strip():  # the whole message was a note: acknowledge it, keep nothing of it
            await asyncio.to_thread(app.store.add_skipped, message.message_id, sender, sent_at, "note",
                                    NOTE_REASON.format(keyword=app.settings.note_keyword))
        else:
            await asyncio.to_thread(app.store.add_message, message.message_id, sender, sent_at, text, attachment)
    except Exception:  # noqa: BLE001
        log.exception("Could not store message %s", message.message_id)
        await message.reply_text("⚠️ Kashio couldn't save this message to the sheet. Edit it (even slightly) to retry.")


async def on_group_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photos, voice messages, files: acknowledged in the inbox, never downloaded, never sent anywhere."""
    app = kashio_of(context)
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or app.store is None or chat.id != app.group_id:
        return
    kind = describe_media(message)
    try:
        await asyncio.to_thread(
            app.store.add_skipped, message.message_id, _name(message.from_user), message.date.astimezone(app.settings.timezone),
            kind, MEDIA_REASON.format(kind=kind),
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not log %s message %s", kind, message.message_id)


async def on_group_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    message = update.edited_message
    if message is None or app.group_id is None or message.chat_id != app.group_id:
        return
    text = message.text or message.caption
    if not text:
        return
    app.connect()
    if app.store is None:
        log.warning("Edit of %s not stored: %s", message.message_id, app.store_error)
        return
    edited_at = (message.edit_date or message.date).astimezone(app.settings.timezone)
    text, had_note = split_note(text, app.settings.note_keyword)
    note_only = had_note and not text.strip()
    reason = NOTE_REASON.format(keyword=app.settings.note_keyword)
    sender, sent_at = _name(message.from_user), message.date.astimezone(app.settings.timezone)
    try:
        status = await asyncio.to_thread(
            app.store.update_message, message.message_id, "[note]" if note_only else text, edited_at, reason if note_only else None
        )
        if status is None:  # never stored, e.g. sent while the bot was offline or the sheet unreachable
            if note_only:
                await asyncio.to_thread(app.store.add_skipped, message.message_id, sender, sent_at, "note", reason)
            else:
                await asyncio.to_thread(app.store.add_message, message.message_id, sender, sent_at, text)
        elif status == STATUS_PENDING_REVISION:
            await message.reply_text(
                "🗑 Noted: this message's rows in the sheet will be removed at the next sync." if note_only else
                "✏️ Noted: this message already has rows in the sheet; they will be updated at the next sync."
            )
    except Exception:  # noqa: BLE001
        log.exception("Could not store edit of message %s", message.message_id)
        await message.reply_text("⚠️ Kashio couldn't save this edit to the sheet. Edit again to retry.")


# ---------------------------------------------------------------- commands


async def may_see_status(app: Kashio, bot: Bot, chat: Chat, user: User | None) -> bool:
    """Group members and the admin see the full checklist; so does anyone while the bot is still unpaired
    (someone has to set it up). A stranger messaging a paired bot privately learns nothing."""
    if chat.type != Chat.PRIVATE or app.group_id is None:
        return True
    return await app.is_household_member(bot, user)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    app.connect()
    if not await may_see_status(app, context.bot, chat, user):
        await message.reply_text(STRANGER_REPLY)
        return
    lines = ["<b>Hi, I'm Kashio.</b> I file the expense notes from your group into your Google Sheet.", ""]
    lines.append(app.status_text(context.bot.username, as_html=True))
    if chat.type == Chat.PRIVATE and user and user.id == app.admin_id:
        lines.append("Sync reports arrive in this chat.")
    lines.append(f"(This chat's id is {chat.id}.)")
    lines.append("")
    lines.append(html.escape(HELP, quote=False))
    await deliver(context.bot, chat.id, "\n".join(lines), as_html=True)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    app.connect(force=True)
    if not await may_see_status(app, context.bot, chat, user):
        await message.reply_text(STRANGER_REPLY)
        return
    await deliver(context.bot, message.chat_id, app.status_text(context.bot.username, as_html=True), as_html=True)


async def on_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None or user is None:
        return
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        await message.reply_text("Run /setup inside the group you want me to record.")
        return
    app.connect(force=True)
    if app.store is None:
        await message.reply_text("I can't save the pairing yet, because the spreadsheet is not reachable:\n"
                                 f"{app.store_error}\nFix that first, then run /setup again.")
        return
    if app.settings.telegram_chat_id is not None:
        same = chat.id == app.settings.telegram_chat_id
        await message.reply_text(
            "The group is fixed by TELEGRAM_CHAT_ID in my environment"
            + (", and it is this group. Nothing to do." if same else ", and it is a different group, so I ignore this one.")
        )
        return
    if app.group_id is not None and app.group_id != chat.id:
        await message.reply_text("I'm already paired with another group. Clear the Bot_Config tab in the spreadsheet to pair me here.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        member = None
    if member is None or member.status not in ADMIN_ROLES:
        await message.reply_text("Only a group admin can run /setup.")
        return
    await asyncio.to_thread(
        app.store.write_config,
        {"group_id": chat.id, "group_title": chat.title or "", "admin_id": user.id, "admin_name": user.first_name},
        user.first_name,
    )
    app.reload_config()
    log.info("Paired with group %s (%r); admin %s (%s)", chat.id, chat.title, user.first_name, user.id)
    await message.reply_text(
        f"Set up. From now on I record every message in this group; sync runs on the schedule and on /sync.\n"
        f"Full reports go to {user.first_name} privately.\n\n" + app.status_text(context.bot.username)
    )
    if not await deliver(context.bot, user.id, "Hi! Your Kashio sync reports will arrive here.\n" + HELP):
        await message.reply_text(f"{user.first_name}, open @{context.bot.username} and press Start so I can message you privately.")


async def on_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    app.connect()
    if chat.type == Chat.PRIVATE:
        if not await app.is_household_member(context.bot, user):
            await message.reply_text("I only work for members of my paired group." if app.group_id else
                                     "I'm not paired with a group yet: a group admin has to type /setup in the group first.")
            return
    elif chat.id != app.group_id:
        if app.group_id is None and app.hint_due(chat.id):
            await message.reply_text("I'm not paired with a group yet. A group admin can type /setup here.")
        return
    if not app.ready:
        await deliver(context.bot, chat.id, "I can't sync yet.\n" + app.status_text(context.bot.username))
        return
    await app.detect_deletions(context.bot, days=MANUAL_PROBE_DAYS)
    report = await app.sync(TRIGGER_MANUAL, _name(user))
    if report is None:
        await message.reply_text("A sync is already running. Give it a minute.")
        return
    full_report = chat.type == Chat.PRIVATE
    await deliver(context.bot, chat.id, format_report(report, as_html=True) if full_report else format_summary(report, as_html=True), as_html=True)
    await app.notify(context.bot, report, already_informed=frozenset({chat.id}))


async def household_only(app: Kashio, bot: Bot, chat: Chat, message: Message, user: User | None) -> bool:
    """Commands that read or change the sheet: the paired group, or a member of it in a private chat."""
    if chat.type == Chat.PRIVATE:
        if not await app.is_household_member(bot, user):
            await message.reply_text("I only work for members of my paired group." if app.group_id else
                                     "I'm not paired with a group yet: a group admin has to type /setup in the group first.")
            return False
        return True
    return chat.id == app.group_id


async def on_report(update: Update, context: ContextTypes.DEFAULT_TYPE, words: list[str] | None = None) -> None:
    """Spending by month, computed from the sheet in code: `/report`, `/report €`, `/report usd`, `/report lira`."""
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None or not message.text:
        return
    app.connect()
    if not await household_only(app, context.bot, chat, message, user):
        return
    if app.store is None:
        await deliver(context.bot, chat.id, "I can't read the sheet right now.\n" + app.status_text(context.bot.username))
        return
    words = message.text.split()[1:] if words is None else words
    currency = app.settings.default_currency
    if words:
        currency = parse_currency(" ".join(words))
        if currency is None:
            await message.reply_text(f"I don't know the currency {' '.join(words)!r}. Try TRY, EUR, USD, GBP or TOMAN, or ₺, €, $, £.")
            return
    months, notes = await asyncio.to_thread(monthly_totals, app.store, app.settings, currency)
    await deliver(context.bot, chat.id, format_monthly_report(months, currency, notes, as_html=True), as_html=True)


class MentionsBot(filters.MessageFilter):
    """A group message that mentions this bot (@botname), wherever in the text."""

    def filter(self, message: Message) -> bool:
        bot = message.get_bot()
        username = getattr(bot, "username", None)
        if not username or not message.text:
            return False
        return any(e.type == MessageEntity.MENTION and message.text[e.offset : e.offset + e.length].casefold() == f"@{username}".casefold()
                   for e in message.entities or ())


async def on_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"@botname" in the group: alone or before a command it is a question to the bot; before an expense note it is noise."""
    message = update.effective_message
    if message is None or not message.text:
        return
    mention = f"@{context.bot.username}"
    rest = re.sub(re.escape(mention), " ", message.text, flags=re.IGNORECASE).split()
    at = next((i for i, w in enumerate(rest) if w.startswith("/")), None)  # the command may follow a few words
    command = rest[at].lower().split("@")[0] if at is not None else ""
    words = rest[at + 1:] if at is not None else rest
    if command in ("/sync",):
        await on_sync(update, context)
    elif command in ("/status",):
        await on_status(update, context)
    elif command in ("/review",):
        await on_review(update, context, words)
    elif command in ("/report",):
        await on_report(update, context, words)
    elif command in ("/backfill",):
        await on_backfill(update, context)
    elif not command and words and any(ch.isdigit() for ch in "".join(words).translate(_ASCII_DIGITS)):
        await on_group_message(update, context)  # an expense note that happens to mention the bot
    else:
        await on_start(update, context)


_ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE, words: list[str] | None = None) -> None:
    """List what waits for a person, or act on the last list: `/review`, `/review keep 2 3`, `/review done 1` or `done all`."""
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None or not message.text:
        return
    app.connect()
    if chat.type == Chat.PRIVATE:
        if not await app.is_household_member(context.bot, user):
            await message.reply_text("I only work for members of my paired group." if app.group_id else
                                     "I'm not paired with a group yet: a group admin has to type /setup in the group first.")
            return
    elif chat.id != app.group_id:
        return
    if app.store is None:
        await deliver(context.bot, chat.id, "I can't read the inbox right now.\n" + app.status_text(context.bot.username))
        return
    words = message.text.split()[1:] if words is None else words
    action = words[0].lower() if words else ""
    if action in ("keep", "done"):
        listed = context.chat_data.get("review") or []
        if not listed:
            await message.reply_text("Send /review first, then refer to the numbers in that list.")
            return
        if len(words) > 1 and words[1].lower() == "all":
            chosen = list(range(1, len(listed) + 1))
        else:
            chosen = [int(w) for w in words[1:] if w.isdigit() and 1 <= int(w) <= len(listed)]
        if not chosen:
            await message.reply_text(f"Say which ones: /review {action} 1 3, or /review {action} all.")
            return
        items = [listed[n - 1] for n in chosen]
        marks = []
        for item in items:
            if action == "keep":
                status = STATUS_PENDING_REVISION if item.rows_added > 0 else STATUS_PENDING
                marks.append((item, status, item.rows_added if item.rows_added else "", f"queued again through /review by {_name(user)}"))
            else:
                marks.append((item, STATUS_RESOLVED, item.rows_added if item.rows_added else "", f"closed through /review by {_name(user)}"))
        await asyncio.to_thread(app.store.mark_messages, marks)
        context.chat_data["review"] = [item for item in listed if item not in items]
        if action == "keep":
            await message.reply_text(f"Queued {len(items)} message(s) for the next sync; /sync runs it now.")
        else:
            await message.reply_text(f"Closed {len(items)} item(s).")
        return
    items = await asyncio.to_thread(app.store.review_items)
    context.chat_data["review"] = items
    if not items:
        await message.reply_text("Nothing waits for a look.")
        return
    lines = [f"<b>🔎 {len(items)} item(s) waiting for a look</b>"]
    for number, item in enumerate(items, start=1):
        excerpt = " | ".join(part.strip() for part in item.text.splitlines() if part.strip())[:60]
        label = "possible duplicate" if item.status == "duplicate" else "needs review"
        lines.append(html.escape(f"{number}. {item.sender} · {item.sent_at:%d %b %H:%M} · “{excerpt}” — {label}: {item.note}", quote=False))
    lines += ["", "/review done 2 closes item 2 (or /review done all); /review keep 2 queues it for the next sync anyway."]
    await deliver(context.bot, chat.id, "\n".join(lines), as_html=True)


async def on_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None or not message.text:
        return
    if chat.type != Chat.PRIVATE:
        await message.reply_text("Please use /backfill in a private chat with me, so pasted history does not mix with live notes.")
        return
    app.connect()
    if not await app.is_household_member(context.bot, user):
        await message.reply_text("Only members of the paired group can import history.")
        return
    if not app.ready:
        await deliver(context.bot, chat.id, "I can't import yet.\n" + app.status_text(context.bot.username))
        return
    parts = message.text.split(maxsplit=1)
    body = parts[1] if len(parts) > 1 else ""
    if body.strip() and len(message.text) < LONG_PASTE_CHARS:
        await process_backfill(app, context.bot, chat.id, _name(user), body)
        return
    if body.strip():  # a long paste right after the command: Telegram has cut it, the rest arrives as further messages
        context.chat_data["capture"] = [body]
        _schedule_auto_finish(context, chat.id)
        await message.reply_text(f"Got the first part. Telegram cuts long pastes into several messages, so I wait {BACKFILL_QUIET_SECONDS} s "
                                 "for the rest; /done starts the import now, /cancel discards.")
        return
    context.chat_data["capture"] = []
    await message.reply_text(
        "Paste the copied messages now, in as many messages as you need (Telegram splits long pastes itself).\n"
        f"I start the import {BACKFILL_QUIET_SECONDS} seconds after the last part arrives, or right away on /done. /cancel discards.\n\n"
        "Expected format, as copied from a Telegram chat:\n"
        "Sam, [1 Sep 2026 at 21:14:10]:\nA101\n2045"
    )


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    capture = context.chat_data.get("capture")
    if capture is None:
        app = kashio_of(context)
        app.connect()
        if not await may_see_status(app, context.bot, update.effective_chat, update.effective_user):
            await message.reply_text(STRANGER_REPLY)
            return
        await deliver(context.bot, message.chat_id, "I only read the group; here I answer commands.\n\n" + app.status_text(context.bot.username) + "\n\n" + HELP)
        return
    capture.append(message.text)
    _schedule_auto_finish(context, message.chat_id)
    await message.reply_text(f"Got it ({len(capture)} part(s)). Importing in {BACKFILL_QUIET_SECONDS} s unless more arrives; /done starts now, /cancel discards.")


def _schedule_auto_finish(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    name = f"backfill-{chat_id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_once(_auto_finish, when=BACKFILL_QUIET_SECONDS, chat_id=chat_id, name=name)


def _cancel_auto_finish(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(f"backfill-{chat_id}"):
        job.schedule_removal()


async def _auto_finish(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    capture = context.application.chat_data[chat_id].pop("capture", None)
    if capture:
        await process_backfill(kashio_of(context), context.bot, chat_id, "paste", "\n".join(capture))


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    _cancel_auto_finish(context, chat.id)
    capture = context.chat_data.pop("capture", None)
    if not capture:
        await message.reply_text("Nothing to import. Send /backfill first, then paste the messages.")
        return
    await process_backfill(app, context.bot, chat.id, _name(user), "\n".join(capture))


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    _cancel_auto_finish(context, message.chat_id)
    had = context.chat_data.pop("capture", None) is not None
    await message.reply_text("Discarded." if had else "Nothing to cancel.")


async def process_backfill(app: Kashio, bot: Bot, chat_id: int, requested_by: str, content: str) -> None:
    """Parse pasted history, queue it, and run a sync right away."""
    if not app.can_sync:
        await deliver(bot, chat_id, "I can't import yet.\n" + app.status_text(bot.username))
        return
    parsed = parse(content, app.settings, ignore_sender=getattr(bot, "first_name", None))
    if not parsed.messages:
        await deliver(bot, chat_id, "I couldn't recognise any messages. Copy them from the Telegram chat so each starts with a line like:\n"
                                    "Sam, [3 Sep 2026 at 09:59:44]:\nfollowed by the message text. Nothing was imported.")
        return
    result = await asyncio.to_thread(import_messages, app.store, app.settings, parsed.messages)
    text = result.describe()
    for line in parsed.notes():
        text += "\n" + line
    await deliver(bot, chat_id, text)
    if not result.imported:
        await deliver(bot, chat_id, "Nothing new to process, so no sync was run.")
        return
    report = await app.sync(TRIGGER_MANUAL, f"{requested_by} (backfill)")
    if report is None:
        await deliver(bot, chat_id, "A sync is already running; the imported messages will be processed when you run /sync.")
        return
    await deliver(bot, chat_id, format_report(report, as_html=True), as_html=True)
    await app.notify(bot, report, already_informed=frozenset({chat_id}))


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Someone added the bot to a chat. While paired, any group other than the paired one is left at once."""
    app = kashio_of(context)
    change = update.my_chat_member
    if change is None or change.chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        return
    joined = change.new_chat_member.status in ("member", "administrator", "restricted")
    if joined and app.group_id is not None and change.chat.id != app.group_id:
        log.warning("Added to a group I am not paired with (%s, %r); leaving it", change.chat.id, change.chat.title)
        try:
            await context.bot.leave_chat(change.chat.id)
        except TelegramError as exc:
            log.warning("Could not leave chat %s: %s", change.chat.id, exc)


async def scheduled_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    app.connect(force=True)
    if not app.ready:
        log.warning("Scheduled sync skipped, not ready:\n%s", "\n".join(app.status_lines()))
        target = app.admin_id or app.group_id
        if target is not None:
            await deliver(context.bot, target, "Scheduled sync skipped.\n" + app.status_text(context.bot.username))
        return
    await app.detect_deletions(context.bot)
    report = await app.sync(TRIGGER_SCHEDULE, "schedule")
    if report is None:
        log.warning("Scheduled sync skipped: another sync is running")
        return
    await app.notify(context.bot, report)


# -------------------------------------------------------------- entry points


def cron_trigger(settings: Settings) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(settings.sync_cron, timezone=settings.timezone.key)
    except ValueError as exc:
        raise ConfigError(f"SYNC_CRON {settings.sync_cron!r} is not a valid crontab expression") from exc


def build_application(app: Kashio) -> Application:
    application = Application.builder().token(app.settings.telegram_bot_token).build()
    application.bot_data["kashio"] = app
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("help", on_start))
    application.add_handler(CommandHandler("status", on_status))
    application.add_handler(CommandHandler("setup", on_setup))
    application.add_handler(CommandHandler("sync", on_sync, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("backfill", on_backfill))
    application.add_handler(CommandHandler("review", on_review))
    application.add_handler(CommandHandler("report", on_report))
    application.add_handler(CommandHandler("done", on_done))
    application.add_handler(CommandHandler("cancel", on_cancel))
    text = (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
    groups_new = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE
    # "@botname", alone or before a command, is addressed to the bot and must not be stored as an expense note
    application.add_handler(MessageHandler(groups_new & filters.TEXT & MentionsBot(), on_mention))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.EDITED_MESSAGE & text, on_group_edit))
    application.add_handler(MessageHandler(groups_new & text, on_group_message))
    application.add_handler(MessageHandler(groups_new & ~filters.TEXT & ~filters.CAPTION & ~filters.StatusUpdate.ALL, on_group_media))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_private_text))
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.job_queue.run_custom(scheduled_sync, job_kwargs={"trigger": cron_trigger(app.settings)}, name="scheduled_sync")
    return application


def start_health_server(app: Kashio, port: int) -> None:
    """GET /health → 200 with a little JSON. Lets Railway (or anyone) see that the process is alive."""
    started = time.time()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if self.path.rstrip("/") not in ("", "/health"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({
                "status": "ok" if app.ready else "degraded",
                "uptime_seconds": int(time.time() - started),
                "paired": app.group_id is not None,
                "syncing": app.lock.locked(),
                "spreadsheet": app.store.spreadsheet.title if app.store else None,
                "problems": [line for line in app.status_lines() if not line.startswith("✅")],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:  # keep the Railway log for real events
            return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        log.warning("Health endpoint not started on port %s: %s", port, exc)
        return
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("Health endpoint listening on port %s (GET /health)", port)


def run_bot(settings: Settings) -> None:
    app = Kashio(settings)  # never fails on missing integrations; it reports them instead
    application = build_application(app)
    start_health_server(app, int(os.environ.get("PORT", "8080")))
    next_run = cron_trigger(settings).get_next_fire_time(None, datetime.now(settings.timezone))
    for line in app.status_lines():
        log.info(line)
    log.info("Kashio is running. Schedule: %s (%s) · next sync: %s", settings.sync_cron, settings.timezone.key, next_run)
    application.run_polling(allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE, Update.MY_CHAT_MEMBER])


def check(settings: Settings) -> int:
    """Verify every connection without spending anything. Returns a process exit code."""
    failures = 0

    def report(label: str, outcome: str, ok: bool = True) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'✓' if ok else '✗'} {label}: {outcome}")

    try:
        me = asyncio.run(Bot(settings.telegram_bot_token).get_me())
        report("Telegram", f"token works, bot is @{me.username}")
    except Exception as exc:  # noqa: BLE001
        report("Telegram", f"{type(exc).__name__}: {exc}", ok=False)

    app = Kashio(settings)
    if app.store is not None:
        source = "GOOGLE_SHEET_ID" if settings.google_sheet_id else "found via the Drive API"
        c = settings.columns
        report("Google Sheets", f"spreadsheet {app.store.spreadsheet.title!r} ({source}), tab {settings.sheet_tab!r}, "
                                f"columns date {c.date} · amount {c.amount} · currency {c.currency} · description {c.description} · "
                                f"category {c.category}, last used row {app.store.last_used_row()}, {len(app.store.pending_messages())} pending")
        categories = clean_categories(app.store.category_options())
        report("Categories", f"{len(categories)} from the column-G dropdown: {', '.join(categories)}")
    else:
        report("Google Sheets", app.store_error or "not connected", ok=False)
    report("Group", f"{app.group_id} ({app.source_of('group_id')})" if app.group_id else
           "not paired: add the bot to the group and type /setup there, or set TELEGRAM_CHAT_ID", ok=app.group_id is not None)
    report("Admin", f"{app.admin_id} ({app.source_of('admin_id')})" if app.admin_id else "not set; reports go to the group")
    if app.claude is not None:
        report("Anthropic", f"key works, model {settings.anthropic_model}, effort {settings.anthropic_effort}")
    else:
        report("Anthropic", app.claude_error or "not connected", ok=False)
    try:
        next_run = cron_trigger(settings).get_next_fire_time(None, datetime.now(settings.timezone))
        report("Schedule", f"{settings.sync_cron!r} ({settings.timezone.key}), next run {next_run:%Y-%m-%d %H:%M}, "
                           f"minimum {settings.scheduled_min_messages} message(s); /sync needs {settings.manual_min_messages}")
    except ConfigError as exc:
        report("Schedule", str(exc), ok=False)

    print("All good." if failures == 0 else f"{failures} problem(s) to fix.")
    return 0 if failures == 0 else 1


async def cli_sync(settings: Settings, dry_run: bool) -> int:
    app = Kashio(settings)
    if not app.can_sync:
        print(app.status_text())
        return 1
    async with Bot(settings.telegram_bot_token) as bot:
        if not dry_run:
            deleted = await app.detect_deletions(bot)
            if deleted:
                print(f"Deleted in Telegram since their rows were written: message ids {deleted}")
        report = await app.sync(TRIGGER_CLI, "terminal", dry_run=dry_run)
        assert report is not None
        print(format_report(report, include_rows=dry_run))
        if not dry_run:
            await app.notify(bot, report)
    return 0


def cli_backfill(settings: Settings, path: str, since: date | None) -> int:
    """Queue older messages from a file: a pasted dump saved as text, or a Telegram Desktop result.json."""
    app = Kashio(settings)
    if app.store is None:
        print(app.status_text())
        return 1
    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    try:
        me = asyncio.run(Bot(settings.telegram_bot_token).get_me())  # to leave the bot's own messages out of the import
        bot_name = me.first_name
    except Exception:  # noqa: BLE001 - not essential
        bot_name = None
    parsed = parse(content, settings, ignore_sender=bot_name)
    if not parsed.messages:
        print("No messages recognised. Expected lines like:  Sam, [3 Sep 2026 at 09:59:44]:  followed by the message text,"
              " or a Telegram Desktop JSON export.")
        return 1
    result = import_messages(app.store, settings, parsed.messages, since)
    print(result.describe())
    for line in parsed.notes():
        print(line)
    if result.imported:
        calls = -(-result.imported // MAX_MESSAGES_PER_CALL)
        print(f"Next: `python bot.py sync --dry-run` to preview, then `python bot.py sync` ({calls} Claude call(s)).")
    return 0


def cli_categorise(settings: Settings, rows: str | None, dry_run: bool, everything: bool = False) -> int:
    """Categorise rows that have a description but no category (or, with `everything`, re-check all); writes only category cells."""
    app = Kashio(settings)
    if not app.can_sync:
        print(app.status_text())
        return 1
    try:
        first, last = (int(part) for part in rows.split(":")) if rows else (2, app.store.last_used_row())
    except ValueError:
        print("--rows expects FIRST:LAST, for example 5:152")
        return 1
    if first < 2 or last < first:
        print("--rows must start at 2 or later and end at or after its start")
        return 1
    found = category_candidates(app.store, first, last, everything)
    if not found:
        print(f"Every row in {first}:{last} with a description already has a category. Nothing to do.")
        return 0
    categories = clean_categories(app.store.category_options())
    print(f"{len(found)} row(s) in {first}:{last} {'to re-check' if everything else 'have a description but no category'}. "
          f"Categories: {', '.join(categories)}")
    result = plan_categories(app.claude, settings, found, categories)
    print(result.describe())
    cost = (result.input_tokens * settings.price_input_per_million + result.output_tokens * settings.price_output_per_million) / 1_000_000
    print(f"Claude: {result.calls} call(s), {result.input_tokens:,} in / {result.output_tokens:,} out, about ${cost:.4f} (effort {settings.anthropic_effort})")
    if dry_run:
        print("Dry run: nothing written. Run again without --dry-run to write these category cells.")
        return 0
    changes = result.changes()
    written = apply_categories(app.store, changes, {c.row: c.current for c in found})
    print(f"Wrote {len(changes)} category cell(s) within {settings.sheet_tab!r}!{written or '-'}; no other cell was touched.")
    return 0 if not result.unanswered else 1


def cli_resync(settings: Settings, since: date | None, dry_run: bool) -> int:
    """Queue every message that owns rows as a revision and run one sync, so its rows follow the current prompt rules."""
    app = Kashio(settings)
    if not app.can_sync:
        print(app.status_text())
        return 1
    noted = app.store.noted_message_ids()
    candidates = [m for m in app.store.messages_with_rows(include_imported=True) if since is None or m.sent_at.date() >= since]
    with_rows = [m for m in candidates if m.message_id in noted]
    if len(with_rows) < len(candidates):
        print(f"Left alone: {len(candidates) - len(with_rows)} message(s) whose rows carry no provenance note (written before notes existed); "
              "they cannot be updated in place.")
    if not with_rows:
        print("No message with rows" + (f" sent on or after {since:%d/%m/%Y}" if since else "") + ". Nothing to do.")
        return 0
    rows = sum(m.rows_added for m in with_rows)
    print(f"{len(with_rows)} message(s) own {rows} row(s)" + (f" since {since:%d/%m/%Y}" if since else "") + "; each will be re-extracted "
          f"and its rows updated in place ({-(-len(with_rows) // MAX_MESSAGES_PER_CALL)} Claude call(s)).")
    if dry_run:
        for m in with_rows:
            print(f"  {m.sent_at:%d/%m/%Y %H:%M} {m.sender}: {' | '.join(part.strip() for part in m.text.splitlines() if part.strip())[:70]} ({m.rows_added} row(s))")
        print("Dry run: nothing queued, nothing written.")
        return 0
    app.store.mark_messages([(m, STATUS_PENDING_REVISION, m.rows_added, "queued by resync so its rows follow the current rules") for m in with_rows])
    return asyncio.run(cli_sync(settings, dry_run=False))


def cli_init_sheet(settings: Settings, rewrite: bool) -> int:
    """Create (or rewrite) the Summary report tab and point the category dropdown at its category list."""
    app = Kashio(settings)
    if app.store is None:
        print(app.status_text())
        return 1
    try:
        print(build_summary(app.store, settings, rewrite=rewrite))
    except FileExistsError as exc:
        print(f"{exc} Add --rewrite to replace it (everything on that tab is lost; the transactions tab is untouched).")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="kashio", description="Telegram expense notes → Google Sheets.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="start the bot (default)")
    commands.add_parser("check", help="verify configuration and access; makes no paid call")
    sync_parser = commands.add_parser("sync", help="run one sync now and exit")
    sync_parser.add_argument("--dry-run", action="store_true", help="call Claude and print the rows; write nothing, message nobody")
    backfill_parser = commands.add_parser("backfill", help="queue older messages from a pasted dump or a Telegram Desktop export; no Claude call")
    backfill_parser.add_argument("file", help="text file with copied messages, or result.json from Telegram Desktop")
    backfill_parser.add_argument("--from", dest="since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                                 help="dismiss everything dated before this day (default: import every day; duplicates are held)")
    init_parser = commands.add_parser("init-sheet", help="build the Summary report tab over the transactions tab")
    init_parser.add_argument("--rewrite", action="store_true", help="replace an existing Summary tab (its exchange rates are kept)")
    cat_parser = commands.add_parser("categorise", help="fill in the empty category cells of rows that have a description; one Claude call")
    cat_parser.add_argument("--rows", metavar="FIRST:LAST", help="row range to look at (default: every row of the tab)")
    cat_parser.add_argument("--dry-run", action="store_true", help="print the proposed categories, write nothing")
    cat_parser.add_argument("--all", dest="everything", action="store_true", help="re-check every row that has a description; rewrite only the categories that change")
    resync_parser = commands.add_parser("resync", help="re-extract messages that already have rows so the rows follow the current prompt rules")
    resync_parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD", help="only messages sent on or after this day")
    resync_parser.add_argument("--dry-run", action="store_true", help="list what would be re-extracted, change nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        settings = Settings.from_env()
        if args.command == "check":
            sys.exit(check(settings))
        elif args.command == "sync":
            sys.exit(asyncio.run(cli_sync(settings, args.dry_run)))
        elif args.command == "backfill":
            sys.exit(cli_backfill(settings, args.file, args.since))
        elif args.command == "init-sheet":
            sys.exit(cli_init_sheet(settings, args.rewrite))
        elif args.command == "categorise":
            sys.exit(cli_categorise(settings, args.rows, args.dry_run, args.everything))
        elif args.command == "resync":
            sys.exit(cli_resync(settings, args.since, args.dry_run))
        else:
            run_bot(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
