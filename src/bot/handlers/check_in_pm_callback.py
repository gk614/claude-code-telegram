"""Callback handler for PM check-in step-by-step flow.

Callback data format: "pm_qN:VALUE"
On tap: store answer, edit message to remove keyboard, send next question.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..features.check_in_pm_flow import (
    _load_state,
    _save_state,
    send_pm_q1,
    send_pm_q2,
    send_pm_q3,
    send_pm_q4,
    send_pm_q5,
    send_pm_q6,
    send_pm_done,
)

logger = structlog.get_logger()


async def handle_pm_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Any = None,
    **_kwargs: Any,
) -> None:
    """Handle pm_qN:VALUE callbacks. Records answer, edits message, sends next."""
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2 or not parts[0].startswith("pm_q"):
        await query.answer()
        return
    q_id = parts[0]  # "pm_q0".."pm_q5"
    value = parts[1]

    settings = settings or context.bot_data.get("settings")
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    answers = state.setdefault("pm_answers", {})

    bot = context.bot
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        await query.answer("Нет chat_id")
        return

    # Q6 — speed_index (Cycle 1)
    if q_id == "pm_q6":
        try:
            n = int(value)
            if not 1 <= n <= 10:
                await query.answer("1-10")
                return
        except ValueError:
            await query.answer()
            return
        await query.answer(f"⚡ {n}/10")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        answers["speed_index"] = n
        state["pm_answers"] = answers
        _save_state(repo, state)
        await send_pm_done(bot, chat_id, repo)
        return

    # Q0 — start / later30 / skip
    if q_id == "pm_q0":
        if value == "start":
            await query.answer("Поехали")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_pm_q1(bot, chat_id, repo)
            return
        elif value == "later30":
            await query.answer("Через 30 мин — пингну")
            # B-P1-4 fix: schedule one-shot async reminder.
            import asyncio

            async def _remind():
                await asyncio.sleep(30 * 60)
                from ..features.check_in_pm_flow import send_pm_q0
                # Only re-send if PM still not answered (no race with manual /checkin pm)
                state2 = _load_state(repo)
                if not state2.get("pm_answered") and state2.get("pm_active_question") in ("0", "skipped", None):
                    await send_pm_q0(bot, chat_id, repo)

            asyncio.create_task(_remind())
            try:
                await query.edit_message_text(
                    "⏰ ОК, пингну через 30 мин. _Если бот рестартует — дёрни `/checkin pm` руками._"
                )
            except Exception:
                pass
            return
        elif value == "skip":
            await query.answer("Skip ОК")
            try:
                await query.edit_message_text(
                    "💤 PM пропущен сегодня. Compliance гейта пострадает, но ок.\n"
                    "_/checkin pm — если передумаешь до полуночи_"
                )
            except Exception:
                pass
            state["pm_active_question"] = "skipped"
            _save_state(repo, state)
            return

    # Q1 — what from plan was done
    elif q_id == "pm_q1":
        labels = {
            "all": "✅ Все 3",
            "2of3": "🟡 2 из 3",
            "1of3": "🟡 1 из 3",
            "none": "❌ Ничего",
            "custom": "✏️ свободное описание ниже",
        }
        answers["plan_done"] = labels.get(value, value)
        state["pm_answers"] = answers
        _save_state(repo, state)
        await query.answer(labels.get(value, "записано"))
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if value == "custom":
            # Force open reply for plan description, before going to q2
            from telegram import ForceReply
            from telegram.constants import ParseMode
            state["pm_active_question"] = "1_custom"
            _save_state(repo, state)
            await bot.send_message(
                chat_id=chat_id,
                text="✏️ Опиши свободно что сделано / не сделано:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=False, input_field_placeholder="Свободный текст"),
            )
            return
        await send_pm_q2(bot, chat_id, repo)
        return

    # Q2 — state 1-10
    elif q_id == "pm_q2":
        try:
            n = int(value)
        except ValueError:
            await query.answer("?")
            return
        answers["state"] = n
        state["pm_answers"] = answers
        _save_state(repo, state)
        emoji = "😞" if n <= 3 else ("😐" if n <= 6 else "😊")
        await query.answer(f"{emoji} {n}/10")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_pm_q3(bot, chat_id, repo)
        return

    # Q4 — tomorrow ack
    elif q_id == "pm_q4":
        labels = {"ok": "✅ ОК идём по плану", "edit": "✏️ скорректирую"}
        answers["tomorrow_ack"] = labels.get(value, value)
        state["pm_answers"] = answers
        _save_state(repo, state)
        await query.answer(labels.get(value, "ОК"))
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if value == "edit":
            from telegram import ForceReply
            from telegram.constants import ParseMode
            state["pm_active_question"] = "4_edit"
            _save_state(repo, state)
            await bot.send_message(
                chat_id=chat_id,
                text="✏️ Что хочешь скорректировать на завтра?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=False),
            )
            return
        await send_pm_q5(bot, chat_id, repo)
        return

    # Unknown
    await query.answer()
    logger.warning("pm_callback: unknown", q_id=q_id, value=value)


async def handle_pm_text_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Any = None,
    **_kwargs: Any,
) -> bool:
    """Process plain-text reply during active PM flow.

    Returns True if message was consumed (don't propagate to other handlers).
    """
    msg = update.effective_message
    if not msg or not msg.text:
        return False

    settings = settings or context.bot_data.get("settings")
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    active = state.get("pm_active_question")
    if not active or active in ("done", "skipped"):
        return False

    answers = state.setdefault("pm_answers", {})
    bot = msg.get_bot()
    chat_id = msg.chat_id
    text = msg.text.strip()

    if active == "1_custom":
        answers["plan_done"] = text
        state["pm_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_pm_q2(bot, chat_id, repo)
        return True

    if active == "3":
        answers["energy"] = text
        state["pm_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_pm_q4(bot, chat_id, repo)
        return True

    if active == "4_edit":
        answers["tomorrow_edit_note"] = text
        state["pm_answers"] = answers
        _save_state(repo, state)
        await msg.reply_text("📝 Записал — обсудим в planning-week")
        await send_pm_q5(bot, chat_id, repo)
        return True

    if active == "5":
        # B-P0-13: reject non-task replies — questions, sarcasm, single sentence
        # without comma-separation. PM q5 expects task list, not a question for AI.
        text_stripped = text.strip()
        looks_like_question = (
            text_stripped.endswith("?")
            or any(text_stripped.lower().startswith(w) for w in ("а ", "что ", "как ", "почему ", "зачем ", "когда ", "где ", "кто "))
        )
        # No commas/newlines AND length > 30 chars → likely free-text, not list
        no_separators = "," not in text_stripped and "\n" not in text_stripped and len(text_stripped) > 30
        if looks_like_question or no_separators:
            await msg.reply_text(
                "❓ Похоже это не список задач, а свободный текст / вопрос.\n\n"
                "PM 5/5 ждёт **задачи на завтра через запятую** — например:\n"
                "`закрыть финплан, бег 5км, обед с Ваней`\n\n"
                "Или /skip если задачи только в Todoist уже.",
                parse_mode="Markdown",
            )
            return True

        # Parse comma-separated tasks → create in Todoist
        tasks_raw = [t.strip() for t in text.replace("\n", ",").split(",") if t.strip()]
        created = []
        try:
            import sys, os
            scripts_path = os.environ.get("GENAOS_REPO_PATH", str(repo)) + "/scripts"
            if scripts_path not in sys.path:
                sys.path.insert(0, scripts_path)
            import todoist_sync  # type: ignore
            for t in tasks_raw:
                tid = todoist_sync.create_task(content=t, due_string="tomorrow")
                if tid:
                    created.append(t)
        except Exception:
            logger.exception("pm_q5: todoist create_task failed")

        answers["tomorrow_tasks"] = created
        state["pm_answers"] = answers
        _save_state(repo, state)

        if created:
            tasks_list = "\n".join(f"  • {t}" for t in created)
            await msg.reply_text(f"📝 Создано в Todoist на завтра ({len(created)}):\n{tasks_list}")
        else:
            await msg.reply_text(f"📝 Записано (но в Todoist не создалось — проверь TODOIST_API_TOKEN)")

        # Cycle 1: chain to q6 (speed_index) instead of done directly
        await send_pm_q6(bot, chat_id, repo)
        return True

    return False
