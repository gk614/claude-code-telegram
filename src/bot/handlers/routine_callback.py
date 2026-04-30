"""Callback handler for AM routine checkbox button taps.

Callback data format: "am_routine:toggle:<slug>"
On tap: flip the boolean in state, edit the message keyboard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..features.check_in_keyboard import (
    ROUTINE_ITEMS,
    build_am_routine_keyboard,
    _load_state,
    _save_state,
)

logger = structlog.get_logger()


async def handle_am_routine_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Any = None,
    **_kwargs: Any,
) -> None:
    """Toggle a routine checkbox on tap, edit message in place."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()  # acknowledge to remove "loading" spinner

    parts = query.data.split(":")
    # "Готово →" button → start step-by-step continuation
    if len(parts) == 2 and parts[0] == "am_routine" and parts[1] == "done":
        from .check_in_am_callback import handle_am_routine_done
        await handle_am_routine_done(update, context, settings=settings, **_kwargs)
        return
    if len(parts) != 3 or parts[0] != "am_routine" or parts[1] != "toggle":
        return
    slug = parts[2]
    valid_slugs = {s for s, _, _ in ROUTINE_ITEMS}
    if slug not in valid_slugs:
        return

    settings = settings or (context.bot_data.get("settings") if context else None)
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    checks = state.setdefault(
        "am_routine_checks", {s: False for s, _, _ in ROUTINE_ITEMS}
    )
    checks[slug] = not bool(checks.get(slug, False))
    _save_state(repo, state)

    new_kb = build_am_routine_keyboard(checks)
    try:
        await query.edit_message_reply_markup(reply_markup=new_kb)
    except Exception:
        logger.exception("am_routine: edit_message_reply_markup failed")

    logger.info("am_routine toggled", slug=slug, new_state=checks[slug],
                user=query.from_user.id if query.from_user else None)
