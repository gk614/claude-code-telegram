"""PM check-in step-by-step flow.

5+1 sequential messages with inline keyboards / ForceReply.
State machine driven via state/check_in_state.json.

Flow:
  q0 — context summary + "Поехали"/"30 мин"/"skip"
  q1 — что из плана сделано (4 кнопки + custom)
  q2 — состояние 1-10 (10 кнопок)
  q3 — что истощало / давало энергию (ForceReply, voice/text)
  q4 — завтра по weekly_plan (ОК / редактировать)
  q5 — план на завтра задачами (ForceReply, parsed → Todoist)
  done — финальный ack
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any, List

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
    from . import _state_io
    return _state_io.load_state(repo)



def _save_state(repo: Path, state: dict) -> None:
    from . import _state_io
    _state_io.save_state(repo, state)



def _today_episodic_key_tasks(repo: Path) -> list[str]:
    """Read today's episodic file, find ⭐ Ключевые сегодня block (or first 3 plan items)."""
    today = datetime.now(UTC).date().isoformat()
    f = repo / "tracks" / "state" / "episodic" / f"{today}.md"
    if not f.exists():
        return []
    text = f.read_text(encoding="utf-8")
    m = re.search(r"### ⭐ Ключевые сегодня\s*\n(.*?)(?=\n###|\n## |\Z)", text, re.DOTALL)
    if m:
        items = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*"):
                cleaned = re.sub(r"<!--\s*todoist:[^>]+-->", "", stripped)
                cleaned = re.sub(r"^[-*\s⭐]+", "", cleaned).strip()
                if cleaned:
                    items.append(cleaned)
        return items[:3]
    # Fallback: first 3 lines of "## План на сегодня"
    m = re.search(r"## План на сегодня\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    if m:
        items = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith("- ["):
                cleaned = re.sub(r"<!--\s*todoist:[^>]+-->", "", stripped)
                cleaned = re.sub(r"^- \[[ x]\]\s*", "", cleaned).strip()
                if cleaned:
                    items.append(cleaned)
            if len(items) >= 3:
                break
        return items
    return []


def _build_context_summary(repo: Path) -> str:
    """Stub for day-in-numbers. Real food/workout/billing reading TBD."""
    today = datetime.now(UTC).date().isoformat()
    parts = [f"📊 *День в цифрах*\n"]
    food_f = repo / "tracks" / "body" / "food" / f"{today}.md"
    if food_f.exists():
        text = food_f.read_text(encoding="utf-8")
        m_kcal = re.search(r"Калории:\s*(\d+)", text)
        m_prot = re.search(r"Белок:\s*([\d.]+)", text)
        if m_kcal:
            parts.append(f"🍽 Калории: ~{m_kcal.group(1)} ккал")
        if m_prot:
            parts.append(f"💪 Белок: ~{m_prot.group(1)} г")
    weight_f = repo / "tracks" / "body" / "weights" / f"{today[:7]}.md"
    if weight_f.exists():
        text = weight_f.read_text(encoding="utf-8")
        m_today = re.search(rf"\|\s*{today}\s*\|\s*([\d.]+)", text)
        if m_today:
            parts.append(f"📊 Вес утром: {m_today.group(1)} кг")
    if len(parts) == 1:
        parts.append("_(food/workout/weight данные за сегодня не найдены)_")
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Q0 — context + start button
# ──────────────────────────────────────────────────────────────────────

async def send_pm_q0(bot: Any, chat_id: int, repo: Path) -> None:
    today = datetime.now(UTC).date().isoformat()
    summary = _build_context_summary(repo)
    text = (
        f"🌙 *PM check-in* — {today}\n\n"
        f"{summary}\n\n"
        f"Готов к рефлексии? 5 вопросов, ~3 мин."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Поехали", callback_data="pm_q0:start")],
        [InlineKeyboardButton("Через 30 мин", callback_data="pm_q0:later30")],
        [InlineKeyboardButton("Skip сегодня", callback_data="pm_q0:skip")],
    ])
    state = _load_state(repo)
    state["pm_sent_at"] = datetime.now(UTC).isoformat()
    state["pm_active_question"] = "0"
    state["pm_answered"] = False
    state["pm_locked"] = False
    state.setdefault("pm_answers", {})
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)
    logger.info("PM q0 sent", chat_id=chat_id, msg=msg.message_id)


# ──────────────────────────────────────────────────────────────────────
# Q1 — what from plan was done
# ──────────────────────────────────────────────────────────────────────

async def send_pm_q1(bot: Any, chat_id: int, repo: Path) -> None:
    keys = _today_episodic_key_tasks(repo)
    if keys:
        keys_text = "\n".join(f"{i+1}. {item}" for i, item in enumerate(keys))
        body = f"Ключевые задачи дня были:\n{keys_text}\n\nЧто сделал?"
    else:
        body = "Что из плана дня сделано?"
    text = f"🌙 *PM 1/5*\n\n{body}"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Все 3", callback_data="pm_q1:all"),
            InlineKeyboardButton("2 из 3", callback_data="pm_q1:2of3"),
        ],
        [
            InlineKeyboardButton("1 из 3", callback_data="pm_q1:1of3"),
            InlineKeyboardButton("Ничего", callback_data="pm_q1:none"),
        ],
        [InlineKeyboardButton("Опишу свободно", callback_data="pm_q1:custom")],
    ])
    state = _load_state(repo)
    state["pm_active_question"] = "1"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    state = _load_state(repo)
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q2 — state 1-10
# ──────────────────────────────────────────────────────────────────────

async def send_pm_q2(bot: Any, chat_id: int, repo: Path) -> None:
    text = "🌙 *PM 2/5*\n\nСостояние сейчас?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(n), callback_data=f"pm_q2:{n}") for n in (1, 2, 3)],
        [InlineKeyboardButton(str(n), callback_data=f"pm_q2:{n}") for n in (4, 5, 6)],
        [InlineKeyboardButton(str(n), callback_data=f"pm_q2:{n}") for n in (7, 8, 9, 10)],
    ])
    state = _load_state(repo)
    state["pm_active_question"] = "2"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    state = _load_state(repo)
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q3 — energy / drained (open, ForceReply)
# ──────────────────────────────────────────────────────────────────────

async def send_pm_q3(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🌙 *PM 3/5*\n\n"
        "Что истощало / давало энергию сегодня?\n\n"
        "🎙 Голос или текст. Что в голове сейчас."
    )
    state = _load_state(repo)
    state["pm_active_question"] = "3"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Голосом или текстом"),
    )
    state = _load_state(repo)
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q4 — tomorrow per weekly_plan (placeholder)
# ──────────────────────────────────────────────────────────────────────

async def send_pm_q4(bot: Any, chat_id: int, repo: Path) -> None:
    # Read weekly_plan if exists; for MVP — placeholder
    text = (
        "🌙 *PM 4/5*\n\n"
        "Завтра по плану недели:\n"
        "  • (weekly\\_plan ещё не настроен)\n"
        "  • План тренировки придёт в 07:30\n"
        "  • AM check-in 09:00\n\n"
        "ОК идём по плану?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ОК, идём по плану", callback_data="pm_q4:ok")],
        [InlineKeyboardButton("Хочу скорректировать", callback_data="pm_q4:edit")],
    ])
    state = _load_state(repo)
    state["pm_active_question"] = "4"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )
    state = _load_state(repo)
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Q5 — tomorrow's tasks (open, parsed → Todoist)
# ──────────────────────────────────────────────────────────────────────

async def send_pm_q5(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🌙 *PM 5/5*\n\n"
        "Какие задачи на завтра в Todoist?\n\n"
        "🎙 Перечисли голосом или текстом, по пунктам.\n"
        "Я создам в Todoist с due=tomorrow.\n\n"
        "Например: «закрыть финплан, купить весы, позвонить Алле»"
    )
    state = _load_state(repo)
    state["pm_active_question"] = "5"
    _save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Перечисли через запятую"),
    )
    state = _load_state(repo)
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Done — final ack + write to episodic
# ──────────────────────────────────────────────────────────────────────

async def send_pm_done(bot: Any, chat_id: int, repo: Path) -> None:
    state = _load_state(repo)
    answers = state.get("pm_answers", {})

    # Compose summary
    plan_done = answers.get("plan_done", "—")
    state_now = answers.get("state", "—")
    energy = answers.get("energy", "—")
    tomorrow_ack = answers.get("tomorrow_ack", "—")
    tasks_count = len(answers.get("tomorrow_tasks", []))

    text = (
        "✅ *PM записан*\n\n"
        f"План дня: {plan_done}\n"
        f"Состояние: {state_now}/10\n"
        f"Истощало/энергия: _записано_\n"
        f"Завтра: {tomorrow_ack}\n"
        f"Задач в Todoist: {tasks_count}\n\n"
        "_Compliance дня посчитается через 30 мин (reward gate)._"
    )

    state["pm_active_question"] = "done"
    state["pm_answered"] = True
    state["pm_completed_at"] = datetime.now(UTC).isoformat()
    _save_state(repo, state)

    # Append to episodic
    today = datetime.now(UTC).date().isoformat()
    episodic = repo / "tracks" / "state" / "episodic" / f"{today}.md"
    episodic.parent.mkdir(parents=True, exist_ok=True)
    if episodic.exists():
        content = episodic.read_text(encoding="utf-8")
    else:
        content = f"---\ndate: {today}\ntype: daily\n---\n\n# {today}\n\n"

    pm_data_lines = (
        f"- План: {plan_done}\n"
        f"- Состояние: {state_now}/10\n"
        f"- Истощало/энергия: {energy}\n"
        f"- Завтра ack: {tomorrow_ack}\n"
        f"- Задач на завтра: {tasks_count}\n"
    )
    pm_block = f"\n## PM рефлексия\n\n{pm_data_lines}"

    # B-P0-2 fix: middleware (check_in_answer._ensure_episodic) pre-creates
    # an empty `## PM рефлексия` stub. Old check `if "## PM рефлексия" not in content`
    # was always False → silent-skip data loss. Now: check for actual data marker.
    pm_section_match = re.search(r"## PM рефлексия\s*\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    # Match only same-line content (between `:` and `\n`).
    has_real_data = bool(pm_section_match and re.search(r"- (План|Состояние|Истощало|Завтра|Задач):[ \t]+\S", pm_section_match.group(1)))

    if not has_real_data:
        if pm_section_match:
            # Replace empty stub section content with our data
            content = re.sub(
                r"(## PM рефлексия\s*\n)(\s*)(?=\n## |\Z)",
                lambda m: m.group(1) + "\n" + pm_data_lines + "\n",
                content, count=1, flags=re.DOTALL,
            )
        else:
            content = content.rstrip("\n") + "\n" + pm_block
        episodic.write_text(content, encoding="utf-8")
        logger.info("PM reflection written to episodic", date=today)
    else:
        logger.warning("PM reflection skipped — section already has data", date=today)

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("PM done", chat_id=chat_id, answers=answers)
