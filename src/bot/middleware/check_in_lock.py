"""Check-in lock middleware.

When am_locked or pm_locked is true in state/check_in_state.json,
any non-command message from the user gets a lock reply instead of
being processed. Lock is cleared when the user sends their check-in answer.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict

import structlog

logger = structlog.get_logger()

_STATE_FILE = "state/check_in_state.json"
_LOCK_MSG_AM = "🔒 Сначала ответь на AM check-in — тогда продолжим."
_LOCK_MSG_PM = "🔒 Сначала ответь на PM check-in — тогда продолжим."


def _state_path(genaos_repo: str) -> Path:
    return Path(genaos_repo) / _STATE_FILE


def _read_state(genaos_repo: str) -> Dict[str, Any]:
    p = _state_path(genaos_repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_state(genaos_repo: str, state: Dict[str, Any]) -> None:
    p = _state_path(genaos_repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _clear_am_lock(genaos_repo: str) -> None:
    state = _read_state(genaos_repo)
    state["am_answered"] = True
    state["am_locked"] = False
    _write_state(genaos_repo, state)


def _clear_pm_lock(genaos_repo: str) -> None:
    state = _read_state(genaos_repo)
    state["pm_answered"] = True
    state["pm_locked"] = False
    _write_state(genaos_repo, state)


async def check_in_lock_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Block non-command messages when check-in lock is active."""
    settings = data.get("settings")
    if not settings:
        return await handler(event, data)

    genaos_repo = getattr(settings, "genaos_repo_path", None)
    if not genaos_repo:
        return await handler(event, data)

    msg = event.effective_message
    if not msg:
        return await handler(event, data)

    # Commands always pass through (lock is lifted by /start etc.)
    if msg.text and msg.text.startswith("/"):
        return await handler(event, data)

    state = _read_state(str(genaos_repo))
    am_locked = state.get("am_locked", False)
    pm_locked = state.get("pm_locked", False)

    if not am_locked and not pm_locked:
        return await handler(event, data)

    # Check if this message looks like a check-in answer
    # Heuristic: if am_pending and user sends any substantive reply -> accept as answer
    am_pending = state.get("am_answered") is False and state.get("am_sent_at")
    pm_pending = state.get("pm_answered") is False and state.get("pm_sent_at")

    text = (msg.text or msg.caption or "").strip()

    if am_locked and am_pending and text:
        # Accept as AM check-in answer — clear lock, let message through
        logger.info("check_in_lock: AM answer received, clearing lock")
        _clear_am_lock(str(genaos_repo))
        return await handler(event, data)

    if pm_locked and pm_pending and text:
        logger.info("check_in_lock: PM answer received, clearing lock")
        _clear_pm_lock(str(genaos_repo))
        return await handler(event, data)

    # Still locked — send lock message
    lock_msg = _LOCK_MSG_PM if pm_locked else _LOCK_MSG_AM
    try:
        await msg.reply_text(lock_msg)
    except Exception:
        logger.exception("check_in_lock: failed to send lock reply")

    return None
