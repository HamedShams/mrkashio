"""Kashio: Telegram entry point, scheduler and command-line tools.

    python bot.py                 start the bot (what Railway runs)
    python bot.py check           verify configuration and access; makes no paid call
    python bot.py sync            run one sync from the terminal and exit
    python bot.py sync --dry-run  call Claude and print the rows, but write nothing and message nobody
    python bot.py backfill FILE   queue older messages from a pasted dump or a Telegram Desktop JSON export

Telegram commands:
    /setup     in the group, once, by a group admin: pairs the bot with that group and with you
    /sync      process everything pending now
    /backfill  in a private chat with the bot: paste copied messages, then /done (or /cancel)
    /start     who am I talking to, and the commands
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime

import anthropic
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot, Chat, Update, User
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from backfill import ImportResult, import_messages, parse
from config import ConfigError, Settings
from extractor import MAX_MESSAGES_PER_CALL, clean_categories, load_prompt
from sheets import STATUS_PENDING, SheetStore
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
)

log = logging.getLogger("kashio")

TELEGRAM_MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message
GROUP_ROLES = ("member", "administrator", "creator")
ADMIN_ROLES = ("administrator", "creator")
HELP = (
    "Commands:\n"
    "/sync — process everything pending now\n"
    "/backfill — paste older messages (private chat), then /done\n"
    "/setup — in the group, once, to pair me with it\n"
    "/start — this message"
)


class Kashio:
    """Long-lived clients, the pairing configuration, and the rule that only one sync runs at a time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = SheetStore(settings)
        self.claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.prompt = load_prompt(settings)
        self.lock = asyncio.Lock()
        self._config: dict[str, str] = {}
        self.reload_config()

    # -- pairing: environment variables win, otherwise what /setup stored in the sheet
    def reload_config(self) -> None:
        self._config = self.store.read_config()

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
            await deliver(bot, admin, format_report(report))
        if (
            self.settings.post_summary
            and group is not None
            and group != admin
            and group not in already_informed
            and report.status in (STATUS_OK, STATUS_FAILED)
        ):
            await deliver(bot, group, format_summary(report))

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


async def deliver(bot: Bot, chat_id: int, text: str) -> bool:
    """Send a message without letting a Telegram failure break the sync that produced it."""
    try:
        while text:
            chunk, text = text[:TELEGRAM_MESSAGE_LIMIT], text[TELEGRAM_MESSAGE_LIMIT:]
            await bot.send_message(chat_id, chunk)
        return True
    except TelegramError as exc:
        hint = " Open a private chat with the bot and press Start first." if "initiate" in str(exc) or "not found" in str(exc).lower() else ""
        log.error("Could not deliver a message to chat %s: %s.%s", chat_id, exc, hint)
        return False


def kashio_of(context: ContextTypes.DEFAULT_TYPE) -> Kashio:
    return context.application.bot_data["kashio"]


def _name(user: User | None) -> str:
    return user.first_name if user else "unknown"


# ---------------------------------------------------------------- group: ingest


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None:
        return
    if app.group_id is None:
        log.info("Not paired yet. Message seen in group %s (%r): a group admin can type /setup there.", chat.id, chat.title)
        return
    if chat.id != app.group_id:
        return
    text = message.text or message.caption
    if not text:
        return
    try:
        await asyncio.to_thread(
            app.store.add_message, message.message_id, _name(message.from_user), message.date.astimezone(app.settings.timezone), text
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not store message %s", message.message_id)
        await message.reply_text("⚠️ Kashio couldn't save this message to the sheet. Edit it (even slightly) to retry.")


async def on_group_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    message = update.edited_message
    if message is None or app.group_id is None or message.chat_id != app.group_id:
        return
    text = message.text or message.caption
    if not text:
        return
    edited_at = (message.edit_date or message.date).astimezone(app.settings.timezone)
    try:
        previous = await asyncio.to_thread(app.store.update_message, message.message_id, text, edited_at)
        if previous is None:  # never stored, e.g. sent while the bot was offline for more than a day
            await asyncio.to_thread(
                app.store.add_message, message.message_id, _name(message.from_user), message.date.astimezone(app.settings.timezone), text
            )
        elif previous != STATUS_PENDING:
            await message.reply_text(
                "⚠️ This message was already synced to the sheet. I recorded the edit but did not change the sheet; "
                "please fix that row by hand."
            )
    except Exception:  # noqa: BLE001
        log.exception("Could not store edit of message %s", message.message_id)
        await message.reply_text("⚠️ Kashio couldn't save this edit to the sheet. Edit again to retry.")


# ---------------------------------------------------------------- commands


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    lines = ["Hi, I'm Kashio. I file the expense notes from your group into your Google Sheet."]
    if chat.type == Chat.PRIVATE:
        if user and user.id == app.admin_id:
            lines.append("Sync reports arrive here.")
        elif app.admin_id is None and app.group_id is None:
            lines.append("I'm not paired with a group yet. Add me to your group and, as a group admin, type /setup there.")
        lines.append(f"(This chat's id is {chat.id}.)")
    else:
        lines.append(
            "I'm recording this group." if chat.id == app.group_id
            else "A group admin can type /setup here to pair me with this group." if app.group_id is None
            else "I'm paired with a different group."
        )
        lines.append(f"(This group's id is {chat.id}.)")
    lines.append(HELP)
    await message.reply_text("\n".join(lines))


async def on_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None or user is None:
        return
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        await message.reply_text("Run /setup inside the group you want me to record.")
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
        f"Full reports go to {user.first_name} privately."
    )
    if not await deliver(context.bot, user.id, "Hi! Your Kashio sync reports will arrive here.\n" + HELP):
        await message.reply_text(f"{user.first_name}, open @{context.bot.username} and press Start so I can message you privately.")


async def on_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    if chat.id != app.group_id and not (chat.type == Chat.PRIVATE and await app.is_household_member(context.bot, user)):
        return
    report = await app.sync(TRIGGER_MANUAL, _name(user))
    if report is None:
        await message.reply_text("A sync is already running. Give it a minute.")
        return
    full_report = chat.type == Chat.PRIVATE
    await deliver(context.bot, chat.id, format_report(report) if full_report else format_summary(report))
    await app.notify(context.bot, report, already_informed=frozenset({chat.id}))


async def on_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None or not message.text:
        return
    if chat.type != Chat.PRIVATE:
        await message.reply_text("Please use /backfill in a private chat with me, so pasted history does not mix with live notes.")
        return
    if not await app.is_household_member(context.bot, user):
        await message.reply_text("Only members of the paired group can import history.")
        return
    body = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    if body.strip():
        await process_backfill(app, context.bot, chat.id, _name(user), body)
        return
    context.chat_data["capture"] = []
    await message.reply_text(
        "Paste the copied messages now, in as many messages as you need (Telegram splits long pastes itself).\n"
        "Send /done when finished, or /cancel to discard.\n\n"
        "Expected format, as copied from a Telegram chat:\n"
        "Shiva ❤️, [1 Sep 2026 at 21:14:10]:\nA101\n2045"
    )


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    capture = context.chat_data.get("capture")
    if capture is None:
        await message.reply_text("I only read the group. " + HELP)
        return
    capture.append(message.text)
    await message.reply_text(f"Got it ({len(capture)} part(s)). Send /done to import, or /cancel.")


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
    chat, message, user = update.effective_chat, update.effective_message, update.effective_user
    if chat is None or message is None:
        return
    capture = context.chat_data.pop("capture", None)
    if not capture:
        await message.reply_text("Nothing to import. Send /backfill first, then paste the messages.")
        return
    await process_backfill(app, context.bot, chat.id, _name(user), "\n".join(capture))


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    had = context.chat_data.pop("capture", None) is not None
    await message.reply_text("Discarded." if had else "Nothing to cancel.")


async def process_backfill(app: Kashio, bot: Bot, chat_id: int, requested_by: str, content: str) -> None:
    """Parse pasted history, queue it, and run a sync right away."""
    messages = parse(content, app.settings)
    if not messages:
        await deliver(bot, chat_id, "I couldn't recognise any messages. Copy them from the Telegram chat so each starts with a line like:\nHamed Shams, [3 Sep 2026 at 09:59:44]:")
        return
    result: ImportResult = await asyncio.to_thread(import_messages, app.store, app.settings, messages)
    await deliver(bot, chat_id, result.describe())
    if not result.imported:
        return
    report = await app.sync(TRIGGER_MANUAL, f"{requested_by} (backfill)")
    if report is None:
        await deliver(bot, chat_id, "A sync is already running; the imported messages will be processed when you run /sync.")
        return
    await deliver(bot, chat_id, format_report(report))
    await app.notify(bot, report, already_informed=frozenset({chat_id}))


async def scheduled_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = kashio_of(context)
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
    application.add_handler(CommandHandler("setup", on_setup))
    application.add_handler(CommandHandler("sync", on_sync))
    application.add_handler(CommandHandler("backfill", on_backfill))
    application.add_handler(CommandHandler("done", on_done))
    application.add_handler(CommandHandler("cancel", on_cancel))
    text = (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.EDITED_MESSAGE & text, on_group_edit))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.MESSAGE & text, on_group_message))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_private_text))
    application.job_queue.run_custom(scheduled_sync, job_kwargs={"trigger": cron_trigger(app.settings)}, name="scheduled_sync")
    return application


def run_bot(settings: Settings) -> None:
    app = Kashio(settings)  # connects to the sheet now, so a bad key fails fast
    application = build_application(app)
    next_run = cron_trigger(settings).get_next_fire_time(None, datetime.now(settings.timezone))
    if app.group_id is None:
        log.warning("Not paired with a group yet: add me to the group and type /setup there as a group admin.")
    log.info("Kashio is running. Group: %s · admin: %s · schedule: %s (%s) · next sync: %s",
             app.group_id, app.admin_id, settings.sync_cron, settings.timezone.key, next_run)
    application.run_polling(allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE])


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

    app: Kashio | None = None
    try:
        app = Kashio(settings)
        report("Google Sheets", f"target tab {settings.sheet_tab!r}, last used row {app.store.last_used_row()}, "
                                f"{len(app.store.pending_messages())} pending")
        categories = clean_categories(app.store.category_options())
        report("Categories", f"{len(categories)} from the column-G dropdown: {', '.join(categories)}")
    except Exception as exc:  # noqa: BLE001
        report("Google Sheets", f"{type(exc).__name__}: {exc}", ok=False)

    if app is not None:
        report("Group", f"{app.group_id} ({app.source_of('group_id')})" if app.group_id else
               "not paired: add the bot to the group and type /setup there, or set TELEGRAM_CHAT_ID", ok=app.group_id is not None)
        report("Admin", f"{app.admin_id} ({app.source_of('admin_id')})" if app.admin_id else "not set; reports go to the group")

    try:
        model = anthropic.Anthropic(api_key=settings.anthropic_api_key).models.retrieve(settings.anthropic_model)
        report("Anthropic", f"key works, model {model.display_name}, effort {settings.anthropic_effort}")
    except Exception as exc:  # noqa: BLE001
        report("Anthropic", f"{type(exc).__name__}: {exc}", ok=False)

    try:
        next_run = cron_trigger(settings).get_next_fire_time(None, datetime.now(settings.timezone))
        report("Schedule", f"{settings.sync_cron!r} ({settings.timezone.key}), next run {next_run:%Y-%m-%d %H:%M}, "
                           f"minimum {settings.scheduled_min_messages} message(s); /sync needs {settings.manual_min_messages}")
    except ConfigError as exc:
        report("Schedule", str(exc), ok=False)

    print("All good." if failures == 0 else f"{failures} problem(s) to fix.")
    return 0 if failures == 0 else 1


async def cli_sync(settings: Settings, dry_run: bool) -> None:
    app = Kashio(settings)
    report = await app.sync(TRIGGER_CLI, "terminal", dry_run=dry_run)
    assert report is not None
    print(format_report(report, include_rows=dry_run))
    if not dry_run:
        async with Bot(settings.telegram_bot_token) as bot:
            await app.notify(bot, report)


def cli_backfill(settings: Settings, path: str, since: date | None) -> int:
    """Queue older messages from a file: a pasted dump saved as text, or a Telegram Desktop result.json."""
    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    messages = parse(content, settings)
    if not messages:
        print("No messages recognised. Expected lines like:  Hamed Shams, [3 Sep 2026 at 09:59:44]:  followed by the message text,"
              " or a Telegram Desktop JSON export.")
        return 1
    result = import_messages(SheetStore(settings), settings, messages, since)
    print(result.describe())
    if result.imported:
        calls = -(-result.imported // MAX_MESSAGES_PER_CALL)
        print(f"Next: `python bot.py sync --dry-run` to preview, then `python bot.py sync` ({calls} Claude call(s)).")
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        settings = Settings.from_env()
        if args.command == "check":
            sys.exit(check(settings))
        elif args.command == "sync":
            asyncio.run(cli_sync(settings, args.dry_run))
        elif args.command == "backfill":
            sys.exit(cli_backfill(settings, args.file, args.since))
        else:
            run_bot(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
