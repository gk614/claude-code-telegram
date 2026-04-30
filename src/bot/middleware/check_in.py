"""AM/PM check-in reply middleware.

Pre-router middleware that intercepts user messages which are replies to
the bot's daily AM/PM check-in pings, captures the raw text into the
episodic file, and short-circuits the chain so the inbox-router does not
re-classify the reply as a journal/idea.

Detection is content-based: if `msg.reply_to_message.text` contains the
AM or PM marker phrases (see `bot.features.check_in._AM_MARKERS`/
`_PM_MARKERS`), the message is treated as a check-in answer. No state
file or message_id tracking is required for this v1 — Telegram's reply
chain is the source of truth.

Behavior:
- No `reply_to_message`             → passthrough.
- `reply_to_message` not a check-in → passthrough.
- `reply_to_message` is AM or PM    → capture, send confirmation, stop chain.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import structlog

from ..features.check_in import (
    capture_check_in_reply,
    confirmation_text,
    detect_check_in_kind,
)

logger = structlog.get_logger()


async def check_in_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """See module docstring."""
    msg = event.effective_message
    if not msg or not msg.text:
        return await handler(event, data)

    reply = getattr(msg, "reply_to_message", None)
    if reply is None:
        return await handler(event, data)

    kind = detect_check_in_kind(reply.text or reply.caption or "")
    if kind is None:
        return await handler(event, data)

    settings = data.get("settings")
    repo_path = getattr(settings, "genaos_repo_path", None) if settings else None
    if repo_path is None:
        # No repo configured — we can't write the episodic file; fall back
        # to letting the message flow through router as before, so it lands
        # somewhere rather than being silently dropped.
        logger.warning(
            "check_in: genaos_repo_path unset; passing reply to router",
            kind=kind,
        )
        return await handler(event, data)

    episodic_dir = repo_path / "tracks" / "state" / "episodic"

    try:
        file_path = capture_check_in_reply(
            kind=kind, raw_text=msg.text, episodic_dir=episodic_dir
        )
    except Exception:
        # Fail-open: if capture breaks, let the router handle the message
        # so the user's input is not lost while we debug.
        logger.exception(
            "check_in: capture failed; passing through to router", kind=kind
        )
        return await handler(event, data)

    try:
        await msg.reply_text(confirmation_text(kind))
    except Exception:
        logger.exception("check_in: confirmation reply failed", kind=kind)

    logger.info(
        "check_in: handled reply",
        kind=kind,
        file=str(file_path),
        user_id=getattr(event.effective_user, "id", None),
    )

    # Stop the chain: middleware wrapper in bot/core.py raises
    # ApplicationHandlerStop when a middleware returns without calling
    # `handler`, which prevents router_middleware (group=1) and message
    # handlers (group=10) from running.
    return None
