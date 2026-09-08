"""Handler wiring: which handler picks up which message. Real telegram objects, no network."""

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import MessageHandler

import bot
from config import Settings

BOT = SimpleNamespace(username="mrkashio_bot")


def update_for(text: str, chat_type: str = Chat.SUPERGROUP, edited: bool = False) -> Update:
    entities = ()
    if text.startswith("/"):
        entities = (MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0])),)
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=-1, type=chat_type),
        from_user=User(id=7, first_name="Hamed", is_bot=False), text=text, entities=entities,
    )
    message.set_bot(BOT)  # CommandHandler compares "/sync@botname" against the bot's username
    return Update(update_id=1, edited_message=message) if edited else Update(update_id=1, message=message)


def first_matching_handler(application, update):
    for handler in application.handlers[0]:
        if handler.check_update(update) not in (None, False):
            return handler
    return None


def test_sync_variants_route_to_the_sync_handler(settings: Settings):
    application = bot.build_application(SimpleNamespace(settings=settings))
    for text in ("/sync", "/sync@mrkashio_bot", "@mrkashio_bot /sync", "@mrkashio_bot  /sync please"):
        handler = first_matching_handler(application, update_for(text))
        assert handler is not None and handler.callback is bot.on_sync, text


def test_plain_group_text_routes_to_ingest_and_edits_to_the_edit_handler(settings: Settings):
    application = bot.build_application(SimpleNamespace(settings=settings))
    handler = first_matching_handler(application, update_for("A101 300"))
    assert isinstance(handler, MessageHandler) and handler.callback is bot.on_group_message
    assert first_matching_handler(application, update_for("A101 350", edited=True)).callback is bot.on_group_edit


def test_private_text_goes_to_the_paste_collector_and_setup_is_a_command(settings: Settings):
    application = bot.build_application(SimpleNamespace(settings=settings))
    assert first_matching_handler(application, update_for("some pasted text", chat_type=Chat.PRIVATE)).callback is bot.on_private_text
    assert first_matching_handler(application, update_for("/setup")).callback is bot.on_setup
