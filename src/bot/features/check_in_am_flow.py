"""AM check-in step-by-step continuation flow.

Routine 4 buttons (existing in check_in_keyboard.py + routine_callback.py)
remain. After tapping "Готово →" — flow continues here:

  q1 — утренний вес (ForceReply)
  q2 — состояние 1-10 (10 buttons)
  q3 — часов сна (5 ranges + custom)
  q4 — 3 главных дела (ForceReply, voice/text)
  q5 — как проведёшь время с близкими (ForceReply)
  done — final ack + write to ## AM check-in

State machine via state/check_in_state.json:
  am_active_question: null | "1" | "2" | "3" | "4" | "5" | "done"
  am_answers: {weight, state, sleep_h, top3, family_time}
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

logger = structlog.get_logger()


def _state_path(repo: Path) -> Path:
    return repo / "state" / "check_in_state.json"


def _load_state(repo: Path) -> dict:
    f = _state_path(repo)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(repo: Path, state: dict) -> None:
    f = _state_path(repo)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# Q1 — утренний вес (ForceReply)
# ──────────────────────────────────────────────────────────────────────

async def send_am_q1(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🌅 *AM 1/5*\n\n"
        "📊 Утренний вес?\n\n"
        "_Просто число (например `80.2`) или `/skip`._"
    )
    state = _load_state(repo)
    state["am_active_question"] = "1"
    state.setdefault("am_answers", {})
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Например 80.2"),
    )
    state = _load_state(repo)
    state["am_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q2 — состояние 1-10
# ──────────────────────────────────────────────────────────────────────

async def send_am_q2(bot: Any, chat_id: int, repo: Path) -> None:
    text = "🌅 *AM 2/5*\n\nСостояние сейчас?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(n), callback_data=f"am_q2:{n}") for n in (1, 2, 3)],
        [InlineKeyboardButton(str(n), callback_data=f"am_q2:{n}") for n in (4, 5, 6)],
        [InlineKeyboardButton(str(n), callback_data=f"am_q2:{n}") for n in (7, 8, 9, 10)],
    ])
    state = _load_state(repo)
    state["am_active_question"] = "2"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    state = _load_state(repo)
    state["am_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q3 — часов сна
# ──────────────────────────────────────────────────────────────────────

async def send_am_q3(bot: Any, chat_id: int, repo: Path) -> None:
    text = "🌅 *AM 3/5*\n\nЧасов сна?"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("<5", callback_data="am_q3:lt5"),
            InlineKeyboardButton("5-6", callback_data="am_q3:5_6"),
            InlineKeyboardButton("6-7", callback_data="am_q3:6_7"),
        ],
        [
            InlineKeyboardButton("7-8", callback_data="am_q3:7_8"),
            InlineKeyboardButton(">8", callback_data="am_q3:gt8"),
        ],
        [InlineKeyboardButton("Точное число", callback_data="am_q3:custom")],
    ])
    state = _load_state(repo)
    state["am_active_question"] = "3"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    state = _load_state(repo)
    state["am_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q4 — 3 главных дела
# ──────────────────────────────────────────────────────────────────────

async def send_am_q4(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🌅 *AM 4/5*\n\n"
        "3 главных дела дня — *стратегические*, не из Todoist.\n\n"
        "Например: «закрыть финплан» / «фокус на семье» / «завершить рефакторинг food-tracker».\n\n"
        "🎙 Голос или текст."
    )
    state = _load_state(repo)
    state["am_active_question"] = "4"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Через запятую или с новой строки"),
    )
    state = _load_state(repo)
    state["am_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q5 — время с близкими
# ──────────────────────────────────────────────────────────────────────

async def send_am_q5(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🌅 *AM 5/5*\n\n"
        "👪 Как проведёшь время с близкими сегодня?\n\n"
        "🎙 Голос или текст."
    )
    state = _load_state(repo)
    state["am_active_question"] = "5"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Свободно"),
    )
    state = _load_state(repo)
    state["am_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Done — финал ack + episodic write
# ──────────────────────────────────────────────────────────────────────

async def send_am_done(bot: Any, chat_id: int, repo: Path) -> None:
    state = _load_state(repo)
    answers = state.get("am_answers", {})
    routine = state.get("am_routine_checks", {})

    routine_done = sum(1 for v in routine.values() if v)
    routine_total = len(routine) or 4

    weight = answers.get("weight", "—")
    state_val = answers.get("state", "—")
    sleep_h = answers.get("sleep_h", "—")
    top3 = answers.get("top3", "—")
    family = answers.get("family_time", "—")

    text = (
        "✅ *AM записан*\n\n"
        f"☀️ Утренняя рутина: {routine_done}/{routine_total}\n"
        f"📊 Вес: {weight}\n"
        f"🌅 Состояние: {state_val}/10 · сон: {sleep_h} ч\n"
        f"🎯 3 дела: _записано_\n"
        f"👪 Время с близкими: _записано_\n\n"
        "_Доброе утро. Поехали 💪_"
    )

    state["am_active_question"] = "done"
    state["am_answered"] = True
    state["am_completed_at"] = datetime.now(UTC).isoformat()
    _save_state(repo, state)

    # Append to episodic
    today = datetime.now(UTC).date().isoformat()
    episodic = repo / "tracks" / "state" / "episodic" / f"{today}.md"
    episodic.parent.mkdir(parents=True, exist_ok=True)
    if episodic.exists():
        content = episodic.read_text(encoding="utf-8")
    else:
        content = f"---\ndate: {today}\ntype: daily\n---\n\n# {today}\n\n"

    am_block = (
        f"\n## AM check-in\n\n"
        f"- Утренняя рутина: {routine_done}/{routine_total}\n"
        f"- Вес: {weight}\n"
        f"- Состояние: {state_val}/10\n"
        f"- Сон: {sleep_h} ч\n"
        f"- 3 главных дела: {top3}\n"
        f"- Время с близкими: {family}\n"
    )
    if "## AM check-in" not in content:
        content = content.rstrip("\n") + "\n" + am_block
        episodic.write_text(content, encoding="utf-8")

    # Persist weight in monthly weights file
    if isinstance(weight, (int, float)) or (isinstance(weight, str) and re.match(r"^\d+(\.\d+)?$", str(weight))):
        wf = repo / "tracks" / "body" / "weights" / f"{today[:7]}.md"
        wf.parent.mkdir(parents=True, exist_ok=True)
        if wf.exists():
            wcontent = wf.read_text(encoding="utf-8")
        else:
            wcontent = (
                f"---\ntype: episodic\ntrack: body\nsubtype: weight\nmonth: {today[:7]}\nunit: kg\n---\n\n"
                f"# Вес — {today[:7]}\n\n"
                "| Дата | Вес | Источник |\n|------|-----|----------|\n"
            )
        if today not in wcontent:
            wcontent = wcontent.rstrip() + f"\n| {today} | {weight} | manual |\n"
            wf.write_text(wcontent, encoding="utf-8")

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("AM done", chat_id=chat_id, answers=answers)
