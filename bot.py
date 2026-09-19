"""Kashio: Telegram entry point, scheduler and command-line tools.

    python bot.py                 start the bot (what Railway runs)
    python bot.py check           verify configuration and access; makes no paid call
    python bot.py sync            run one sync from the terminal and exit
    python bot.py sync --dry-run  call Claude and print the rows, but write nothing and message nobody
    python bot.py backfill FILE   queue older messages from a pasted dump or a Telegram Desktop JSON export
    python bot.py init-sheet      build the Summary report tab (add --rewrite to replace an existing one)

Telegram commands:
    /setup     in the group, once, by a group admin: pairs the bot with that group and with you
    /sync      process everything pending now (also accepted as "@botname /sync")
    /backfill  in a private chat with the bot: paste copied messages; the import starts by itself after a short
               pause, or immediately on /done (/cancel discards)
    /status    what is connected and what still needs setting up
    /start     who am I talking to, and the commands

Deleted messages: Telegram sends bots no event for a deletion, so before each sync the bot asks Telegram to
clear its (non-existent) reaction on every message that has rows in the sheet; a deleted message answers
"not found" and its rows are removed at that sync. No visible side effect on existing messages.

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
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
from apscheduler.triggers.cron import CronTrigger
from gspread.exceptions import APIError
from telegram import Bot, Chat, Message, Update, User
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from backfill import import_messages, parse
from summary import build_summary
from config import ConfigError, Settings
from extractor import MAX_MESSAGES_PER_CALL, clean_categories, load_prompt
from sheets import STATUS_PENDING_REVISION, STATUS_SKIPPED, SheetStore
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
RECONNECT_EVERY_SECONDS = 60  # how often a missing integration is retried
ANTHROPIC_MAX_RETRIES = 3  # SDK retries on 408/409/429/5xx and connection errors with exponential backoff (0.5 s → 8 s)
DELETION_PROBE_PAUSE = 0.05  # seconds between Telegram probes, well under the rate limit
HINT_EVERY_SECONDS = 3600  # how often the group is reminded that something is not set up
GROUP_ROLES = ("member", "administrator", "creator")
ADMIN_ROLES = ("administrator", "creator")
HELP = (
    "Commands:\n"
    "/sync — process everything pending now\n"
    "/backfill — paste older messages (private chat); import starts after a short pause or on /done\n"
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

    async def detect_deletions(self, bot: Bot) -> list[int]:
        """Find messages that were deleted in Telegram after their rows were written, and queue their rows for removal.

        Telegram sends no event for deletions. Clearing the bot's reaction on a message is a harmless probe:
        an existing message answers "Reaction_empty", a deleted one "Message to react not found".
        """
        if self.store is None or self.group_id is None:
            return []
        candidates = await asyncio.to_thread(self.store.messages_with_rows)
        deleted: list[tuple[int, int]] = []
        unknown = 0
        for inbox_row, message_id in candidates:
            try:
                await bot.set_message_reaction(self.group_id, message_id, reaction=[])
            except BadRequest as exc:
                text = str(exc).lower()
                if "chat not found" in text:
                    log.warning("Deletion check aborted: Telegram cannot find the group (%s)", exc)
                    return []
                if "message to react not found" in text or "message not found" in text:
                    deleted.append((inbox_row, message_id))
                elif "reaction_empty" not in text and "reaction is invalid" not in text:
                    unknown += 1
                    log.warning("Deletion check: unexpected answer for message %s: %s", message_id, exc)
            except TelegramError as exc:
                unknown += 1
                log.warning("Deletion check: could not probe message %s: %s", message_id, exc)
            await asyncio.sleep(DELETION_PROBE_PAUSE)
        if deleted and len(deleted) == len(candidates) and len(candidates) >= 3:
            log.warning("Deletion check: every one of %d messages looks deleted; that cannot be right, ignoring", len(candidates))
            return []
        if deleted:
            await asyncio.to_thread(self.store.mark_deleted, [row for row, _ in deleted])
            log.info("Deleted in Telegram: message ids %s", [mid for _, mid in deleted])
        return [mid for _, mid in deleted]

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
    """Send a message without letting a Telegram failure break the sync that produced it."""
    try:
        while text:
            cut = TELEGRAM_MESSAGE_LIMIT
            if as_html and len(text) > cut:  # never split inside a tag or an entity
                cut = max(text.rfind("\n", 0, cut), 1)
            chunk, text = text[:cut], text[cut:]
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML if as_html else None)
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
    await app.detect_deletions(context.bot)
    report = await app.sync(TRIGGER_MANUAL, _name(user))
    if report is None:
        await message.reply_text("A sync is already running. Give it a minute.")
        return
    full_report = chat.type == Chat.PRIVATE
    await deliver(context.bot, chat.id, format_report(report, as_html=True) if full_report else format_summary(report, as_html=True), as_html=True)
    await app.notify(context.bot, report, already_informed=frozenset({chat.id}))


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
    if body.strip():
        await process_backfill(app, context.bot, chat.id, _name(user), body)
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
    parsed = parse(content, app.settings)
    if not parsed.messages:
        await deliver(bot, chat_id, "I couldn't recognise any messages. Copy them from the Telegram chat so each starts with a line like:\n"
                                    "Sam, [3 Sep 2026 at 09:59:44]:\nfollowed by the message text. Nothing was imported.")
        return
    result = await asyncio.to_thread(import_messages, app.store, app.settings, parsed.messages)
    text = result.describe()
    if parsed.problems():
        text += "\n" + parsed.problems()
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
    application.add_handler(CommandHandler("done", on_done))
    application.add_handler(CommandHandler("cancel", on_cancel))
    # "@botname /sync" is not a Telegram command (commands start with "/"), but people type it; treat it as /sync.
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.Regex(r"(?i)^@\w+\s*/sync\b"), on_sync))
    text = (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
    groups_new = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE
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
    parsed = parse(content, settings)
    if not parsed.messages:
        print("No messages recognised. Expected lines like:  Sam, [3 Sep 2026 at 09:59:44]:  followed by the message text,"
              " or a Telegram Desktop JSON export.")
        return 1
    result = import_messages(app.store, settings, parsed.messages, since)
    print(result.describe())
    if parsed.problems():
        print(parsed.problems())
    if result.imported:
        calls = -(-result.imported // MAX_MESSAGES_PER_CALL)
        print(f"Next: `python bot.py sync --dry-run` to preview, then `python bot.py sync` ({calls} Claude call(s)).")
    return 0


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
                                 help="import from this day on (default: the day after the sheet's last recorded date)")
    init_parser = commands.add_parser("init-sheet", help="build the Summary report tab over the transactions tab")
    init_parser.add_argument("--rewrite", action="store_true", help="replace an existing Summary tab (its contents are lost)")
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
        else:
            run_bot(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
