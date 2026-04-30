"""Inbox-router middleware: classify text messages via GenaOS Haiku router.

Imports `inbox_router` from the GenaOS repo (path injected into sys.path at
bot startup via core.py:_inject_genaos_path() based on settings.genaos_repo_path).

Behavior:
- ROUTER_ENABLED=false → no-op, pass through.
- Non-text or command messages → pass through.
- Forwarded messages: held 10 s. If a follow-up comment arrives in that
  window the two are bundled (forward = content, comment = routing hint).
  If no comment arrives the forward is routed standalone.
- Category in {idea, task, journal, metric, food, bookmark, routing_unsure}
  → router writes to file, middleware sends transparency reply, chain stops.
- Category in {question, mixed, api_error} → middleware passes through to
  agentic_text handler (Sonnet/Opus answers as before).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

# key=(chat_id, user_id) → {task, text, source_label, msg_id}
_pending_forwards: Dict[Tuple[int, int], Dict[str, Any]] = {}
_FORWARD_HOLD_SECS = 10


async def _route_and_reply(text: str, msg: Any, user: Any, chat: Any) -> bool:
    """Run inbox_router.process() and send transparency reply.

    Returns True if the message was fully handled (no pass-through needed).
    """
    try:
        from inbox_router import process  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.error("inbox_router not importable; check GENAOS_REPO_PATH", error=str(exc))
        return False

    try:
        result = process(
            text=text,
            user_id=user.id,
            message_id=msg.message_id,
            chat_id=chat.id,
            ts=datetime.now(UTC),
        )
    except Exception:
        logger.exception("inbox_router.process() crashed; falling through")
        return False

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

    return not result.passed_through


async def router_middleware(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Pre-filter middleware that classifies text via GenaOS inbox_router."""
    settings = data.get("settings")
    if not settings or not getattr(settings, "router_enabled", False):
        return await handler(event, data)

    msg = event.effective_message
    if not msg:
        return await handler(event, data)

    user = event.effective_user
    chat = event.effective_chat

    # Inverse 10s window: text classified → photo arrives within window.
    # Attach photo to last action's file row, halt chain.
    if user and chat and msg.photo and not msg.text:
        try:
            from inbox_router import attach_to_last_action
            from pathlib import Path as _Path
            import os as _os, base64 as _b64
            # Save the photo bytes so the attachment chip can reference it
            genaos_root = _Path(_os.environ.get("GENAOS_REPO_PATH", "/root/GenaOS"))
            photos_dir = genaos_root / "inbox" / "photos"
            photos_dir.mkdir(parents=True, exist_ok=True)
            largest = msg.photo[-1]
            photo_file = await largest.get_file()
            ext = "jpg"
            photo_path = photos_dir / f"{msg.message_id}.{ext}"
            await photo_file.download_to_drive(custom_path=str(photo_path))
            rel_path = f"inbox/photos/{msg.message_id}.{ext}"
            attached = attach_to_last_action(user.id, rel_path, window_seconds=10)
            if attached:
                logger.info("inverse-window: photo attached to last action", path=rel_path)
                try:
                    await msg.reply_text(f"📎 Прикрепил фото к предыдущей записи")
                except Exception:
                    logger.exception("inverse-window: ack reply failed")
                return None
        except Exception:
            logger.exception("inverse-window: attach failed; falling through")
        return await handler(event, data)

    if not msg.text:
        return await handler(event, data)

    if msg.text.startswith("/"):
        return await handler(event, data)

    if msg.text.startswith("??") or msg.text.startswith("?h "):
        return await handler(event, data)

    user = event.effective_user
    chat = event.effective_chat
    if not user or not chat:
        return await handler(event, data)

    key = (chat.id, user.id)

    # Reply to a bot message → user is talking to the bot, not capturing
    # a new note. Skip router entirely so Sonnet can answer conversationally.
    reply_msg = getattr(msg, "reply_to_message", None)
    if reply_msg is not None:
        reply_from = getattr(reply_msg, "from_user", None)
        if reply_from is not None and getattr(reply_from, "is_bot", False):
            # Don't bundle a forward through this path either — it would
            # have been a `forward_origin` check on `reply_msg` lower down.
            forward_origin_on_reply = (
                getattr(reply_msg, "forward_origin", None)
                or getattr(reply_msg, "forward_from", None)
                or getattr(reply_msg, "forward_from_chat", None)
                or getattr(reply_msg, "forward_date", None)
            )
            if forward_origin_on_reply is None:
                logger.info("router: reply-to-bot detected, skipping router")
                return await handler(event, data)

    # Detect directly forwarded messages
    is_forward = bool(
        getattr(msg, "forward_origin", None)
        or getattr(msg, "forward_from", None)
        or getattr(msg, "forward_from_chat", None)
        or getattr(msg, "forward_date", None)
    )

    if is_forward:
        # Cancel any existing pending forward for this user
        if key in _pending_forwards:
            _pending_forwards[key]["task"].cancel()

        forward_text = msg.text.strip()
        source_label = _describe_forward_origin(msg)

        async def _process_standalone() -> None:
            try:
                await asyncio.sleep(_FORWARD_HOLD_SECS)
            except asyncio.CancelledError:
                return  # bundled with a comment — skip standalone
            _pending_forwards.pop(key, None)
            route_text = f"[Forward от {source_label}]: {forward_text}"
            logger.info("router: standalone forward (no comment in 10s window)", source=source_label)
            await _route_and_reply(route_text, msg, user, chat)

        task = asyncio.create_task(_process_standalone())
        _pending_forwards[key] = {
            "task": task,
            "text": forward_text,
            "source_label": source_label,
            "msg_id": msg.message_id,
        }
        return None  # Hold — do not call handler yet

    # Check if this message follows a pending forward (bundle them)
    pending = _pending_forwards.pop(key, None)
    if pending is not None:
        pending["task"].cancel()
        forward_text = pending["text"]
        source_label = pending["source_label"]
        bundled_text = msg.text
        if forward_text:
            bundled_text = (
                f"{msg.text}\n\n"
                f"Источник (forward от {source_label}):\n{forward_text}"
            )
        logger.info(
            "router: forward+comment bundle (10s window)",
            comment_preview=msg.text[:60],
            forward_len=len(forward_text),
        )
        fully_handled = await _route_and_reply(bundled_text, msg, user, chat)
        if not fully_handled:
            return await handler(event, data)
        return None

    # Normal message — also handle Telegram reply-to-forward bundle
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

    fully_handled = await _route_and_reply(bundled_text, msg, user, chat)
    if not fully_handled:
        return await handler(event, data)
    return None


def _describe_forward_origin(msg: Any) -> str:
    """Best-effort human label for where a forwarded message came from."""
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
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
    """Build Telegram InlineKeyboardMarkup from router serialized payload."""
    rows = []
    for row in payload.get("buttons", []):
        rows.append(
            [InlineKeyboardButton(text=b["text"], callback_data=b["data"]) for b in row]
        )
    return InlineKeyboardMarkup(rows)
