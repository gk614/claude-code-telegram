"""Callback + text-reply handlers for AM check-in step-by-step continuation.

Patterns: am_q1..am_q5, am_routine:done (start of step-by-step).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..features.check_in_am_flow import (
    _load_state,
    _save_state,
    send_am_q1,
    send_am_q2,
    send_am_q3,
    send_am_q4,
    send_am_q5,
    send_am_done,
)

logger = structlog.get_logger()


async def handle_am_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Any = None,
    **_kwargs: Any,
) -> None:
    """Handle am_qN:VALUE callbacks. Records answer, edits message, sends next."""
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2 or not parts[0].startswith("am_q"):
        await query.answer()
        return
    q_id = parts[0]
    value = parts[1]

    settings = settings or context.bot_data.get("settings")
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    answers = state.setdefault("am_answers", {})

    bot = context.bot
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        await query.answer("Нет chat_id")
        return

    # Q2 — состояние
    if q_id == "am_q2":
        try:
            n = int(value)
        except ValueError:
            await query.answer("?")
            return
        answers["state"] = n
        state["am_answers"] = answers
        _save_state(repo, state)
        emoji = "😞" if n <= 3 else ("😐" if n <= 6 else "😊")
        await query.answer(f"{emoji} {n}/10")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_am_q3(bot, chat_id, repo)
        return

    # Q3 — сон
    if q_id == "am_q3":
        # D-P1-3: numeric values are mid-range, label shows visually as range.
        ranges = {
            "lt5": (4.5, "<5 ч"),
            "5_6": (5.5, "5.5 ч"),
            "6_7": (6.5, "6.5 ч"),
            "7_8": (7.5, "7.5 ч"),
            "gt8": (8.5, "8.5+ ч"),
        }
        if value == "custom":
            from telegram import ForceReply
            from telegram.constants import ParseMode
            state["am_active_question"] = "3_custom"
            _save_state(repo, state)
            await query.answer("Точное число")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await bot.send_message(
                chat_id=chat_id,
                text="🌅 *AM 3/5* — точное число часов сна?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=False, input_field_placeholder="Например 7.5"),
            )
            return
        if value in ranges:
            val_num, val_label = ranges[value]
            answers["sleep_h"] = val_num
            answers["sleep_h_label"] = val_label
            state["am_answers"] = answers
            _save_state(repo, state)
            await query.answer(f"😴 {val_label} ч")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_am_q4(bot, chat_id, repo)
            return

    await query.answer()
    logger.warning("am_callback: unknown", q_id=q_id, value=value)


async def handle_am_routine_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Any = None,
    **_kwargs: Any,
) -> None:
    """Triggered by 'Готово →' in routine keyboard. Starts step-by-step flow."""
    query = update.callback_query
    if not query:
        return
    await query.answer("Поехали")

    settings = settings or context.bot_data.get("settings")
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    bot = context.bot
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return

    # Lock keyboard
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_am_q1(bot, chat_id, repo)


async def handle_am_text_reply(
    update_or_event: Any,
    context: Any = None,
    settings: Any = None,
    **_kwargs: Any,
) -> bool:
    """Process plain-text reply during active AM flow.

    Returns True if message consumed.
    """
    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False

    settings = settings or context.bot_data.get("settings")
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    active = state.get("am_active_question")
    if not active or active in ("done", "skipped"):
        return False

    answers = state.setdefault("am_answers", {})
    bot = msg.get_bot()
    chat_id = msg.chat_id
    text = msg.text.strip()

    # Q1 — weight
    if active == "1":
        if text.lower() == "/skip":
            answers["weight"] = "skip"
        else:
            m = re.match(r"^\s*(\d{2,3}(?:\.\d{1,2})?)\s*", text)
            if m:
                answers["weight"] = float(m.group(1))
            else:
                answers["weight"] = text  # store raw
        state["am_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_am_q2(bot, chat_id, repo)
        return True

    if active == "3_custom":
        m = re.match(r"^\s*(\d+(?:\.\d+)?)", text)
        if m:
            answers["sleep_h"] = float(m.group(1))
            answers["sleep_h_label"] = m.group(1)
        else:
            answers["sleep_h"] = text
            answers["sleep_h_label"] = text
        state["am_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_am_q4(bot, chat_id, repo)
        return True

    if active == "4":
        answers["top3"] = text
        state["am_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_am_q5(bot, chat_id, repo)
        return True

    if active == "5":
        answers["family_time"] = text
        state["am_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_am_done(bot, chat_id, repo)
        return True

    return False
