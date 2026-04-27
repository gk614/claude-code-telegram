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
            text=msg.text,
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


def _build_keyboard(payload: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Build Telegram InlineKeyboardMarkup from router's serialized payload."""
    rows = []
    for row in payload.get("buttons", []):
        rows.append(
            [InlineKeyboardButton(text=b["text"], callback_data=b["data"]) for b in row]
        )
    return InlineKeyboardMarkup(rows)
