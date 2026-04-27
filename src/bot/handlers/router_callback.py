"""Callback handler for routing_unsure inline keyboard taps.

Triggered when the user taps a button on the keyboard sent by router_middleware
when category was `routing_unsure`. Calls inbox_router.apply_manual_override()
which writes the message to the user-selected category.
"""

from datetime import UTC, datetime

import structlog
from telegram import Message, Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


async def handle_router_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``router:assign:<category>:<msg_id>`` and ``router:discard:<msg_id>``."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != "router":
        return

    settings = context.bot_data.get("settings")
    if not settings or not getattr(settings, "router_enabled", False):
        return

    # The original user message was replied-to when we sent the keyboard,
    # so it's accessible as query.message.reply_to_message. Note that
    # query.message is typed as MaybeInaccessibleMessage; only the concrete
    # Message subclass has reply_to_message.
    keyboard_msg = query.message if isinstance(query.message, Message) else None
    original_msg = keyboard_msg.reply_to_message if keyboard_msg else None
    raw_text = original_msg.text if original_msg and original_msg.text else ""

    user_id = query.from_user.id if query.from_user else 0
    chat_id = keyboard_msg.chat.id if keyboard_msg else 0

    try:
        from inbox_router import apply_manual_override  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.error("inbox_router not importable for callback", error=str(exc))
        return

    action = parts[1]
    try:
        if action == "discard":
            message_id = int(parts[2])
            result = apply_manual_override(
                category="discard",
                raw_text=raw_text,
                user_id=user_id,
                message_id=message_id,
                chat_id=chat_id,
                ts=datetime.now(UTC),
            )
        elif action == "assign" and len(parts) >= 4:
            category = parts[2]
            message_id = int(parts[3])
            result = apply_manual_override(
                category=category,
                raw_text=raw_text,
                user_id=user_id,
                message_id=message_id,
                chat_id=chat_id,
                ts=datetime.now(UTC),
            )
        else:
            return
    except Exception:
        logger.exception("router callback override failed")
        return

    if result.transparency_reply:
        try:
            # Replace the keyboard message with the resolution
            await query.edit_message_text(result.transparency_reply)
        except Exception:
            logger.exception("router callback edit_message_text failed")
