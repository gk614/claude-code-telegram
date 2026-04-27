"""Inbox-router middleware: classify text messages via GenaOS Haiku router.

Imports `inbox_router` from the GenaOS repo (path injected into sys.path at
bot startup via core.py:_inject_genaos_path() based on settings.genaos_repo_path).

Behavior:
- ROUTER_ENABLED=false → no-op, pass through.
- Non-text or command messages → pass through.
- Category in {idea, task, journal, metric, food, bookmark, routing_unsure}
  → router writes to file, middleware sends transparency reply, chain stops.
- Category in {question, mixed, api_error} → middleware passes through to
  agentic_text handler (Sonnet/Opus answers as before).
"""

from datetime import UTC, datetime
from typing import Any, Callable, Dict

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()


async def router_middleware(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Pre-filter middleware that classifies text via GenaOS inbox_router.

    See module docstring for behavior summary.
    """
    settings = data.get("settings")
    if not settings or not getattr(settings, "router_enabled", False):
        return await handler(event, data)

    msg = event.effective_message
    if not msg or not msg.text:
        return await handler(event, data)

    if msg.text.startswith("/"):
        # Slash commands are handled by their own CommandHandlers; never route.
        return await handler(event, data)

    if msg.text.startswith("??") or msg.text.startswith("?h "):
        # Two escape hatches that bypass the router and pick a non-default model:
        #   - `??`  → Opus 4.x (slow + expensive, for hard reasoning)
        #   - `?h ` → Haiku 4.5 (fast + cheap, for simple lookups)
        # Both pass straight to agentic_text — that handler strips the prefix
        # and swaps config.claude_model accordingly.
        return await handler(event, data)

    # Forward + reply bundle: if this message is a reply to an earlier
    # forwarded message, the user is commenting on that forward
    # ("идея на основе этого поста"). Bundle the comment with the forward
    # body so the router sees the full context. The user's text remains the
    # primary categorisation signal; the forward is appended as a quoted
    # source block. Final formatting (separate column? blockquote?) is a
    # decision for the protocol session — for now both go into the verbatim
    # original column of the table.
    bundled_text = msg.text
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None:
        forward_origin = (
            getattr(reply, "forward_origin", None)
            or getattr(reply, "forward_from", None)
            or getattr(reply, "forward_from_chat", None)
            or getattr(reply, "forward_date", None)
        )
        if forward_origin is not None:
            forward_body = (reply.text or reply.caption or "").strip()
            source_label = _describe_forward_origin(reply)
            if forward_body:
                bundled_text = (
                    f"{msg.text}\n\n"
                    f"Источник (forward от {source_label}):\n{forward_body}"
                )
            logger.info(
                "router: forward+reply bundle detected",
                comment_len=len(msg.text),
                forward_len=len(forward_body),
                source=source_label,
            )

    user = event.effective_user
    chat = event.effective_chat
    if not user or not chat:
        return await handler(event, data)

    try:
        from inbox_router import process  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.error(
            "inbox_router not importable; check GENAOS_REPO_PATH", error=str(exc)
        )
        return await handler(event, data)

    try:
        result = process(
            text=bundled_text,
            user_id=user.id,
            message_id=msg.message_id,
            chat_id=chat.id,
            ts=datetime.now(UTC),
        )
    except Exception:
        logger.exception("inbox_router.process() crashed; falling through")
        return await handler(event, data)

    logger.info(
        "router decision",
        category=result.category,
        confidence=result.confidence,
        passed_through=result.passed_through,
        target=result.target_file,
    )

    if result.transparency_reply:
        try:
            kwargs: Dict[str, Any] = {}
            if result.keyboard_payload:
                kwargs["reply_markup"] = _build_keyboard(result.keyboard_payload)
            await msg.reply_text(result.transparency_reply, **kwargs)
        except Exception:
            logger.exception("router transparency reply failed")

    if result.cost_alert:
        try:
            await msg.reply_text(result.cost_alert)
        except Exception:
            logger.exception("router cost alert send failed")

    if result.passed_through:
        return await handler(event, data)
    # Router fully handled the message — do not call handler; middleware wrapper
    # in core.py will raise ApplicationHandlerStop to halt the chain.
    return None


def _describe_forward_origin(msg: Any) -> str:
    """Best-effort human label for where a forwarded message came from."""
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
        # python-telegram-bot v20+: MessageOriginUser / MessageOriginChannel etc.
        if hasattr(fo, "sender_user") and fo.sender_user:
            return fo.sender_user.full_name or fo.sender_user.username or "user"
        if hasattr(fo, "chat") and fo.chat:
            return getattr(fo.chat, "title", None) or getattr(fo.chat, "username", "channel")
        if hasattr(fo, "sender_user_name") and fo.sender_user_name:
            return fo.sender_user_name
    user = getattr(msg, "forward_from", None)
    if user is not None:
        return user.full_name or user.username or "user"
    chat = getattr(msg, "forward_from_chat", None)
    if chat is not None:
        return chat.title or chat.username or "channel"
    return "external"


def _build_keyboard(payload: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Build Telegram InlineKeyboardMarkup from router's serialized payload."""
    rows = []
    for row in payload.get("buttons", []):
        rows.append(
            [InlineKeyboardButton(text=b["text"], callback_data=b["data"]) for b in row]
        )
    return InlineKeyboardMarkup(rows)
