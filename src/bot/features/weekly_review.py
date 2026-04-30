"""weekly-review — Sunday 18:00 aggregator + reflection flow.

Compact MVP:
  Phase 1 — auto aggregate всех источников за прошедшую неделю
  Phase 2 — send card to user + button "Поехали к рефлексии"
  Phase 3 — 5 questions sequential (3 win / 3 miss / 1 lesson / 1 pattern / 1 experiment)
  Phase 4 — write tracks/state/weekly_summaries/<monday>.md, trigger planning-week
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
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


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _import_todoist(repo: Path):
    scripts_path = str(repo / "scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    import todoist_sync  # type: ignore
    return todoist_sync


def _last_week_range(today: date) -> tuple[date, date]:
    """Monday..Sunday of the week ending yesterday or today (Sunday inclusive)."""
    if today.weekday() == 6:  # Sunday — last 7 days inclusive
        end = today
    else:
        days_back = today.weekday() + 1
        end = today - timedelta(days=days_back)
    start = end - timedelta(days=6)
    return start, end


def aggregate(repo: Path, today: Optional[date] = None) -> dict:
    today = today or datetime.now(UTC).date()
    start, end = _last_week_range(today)
    days = [start + timedelta(days=i) for i in range(7)]

    body = {"strength": 0, "run": 0, "core_days": 0, "weight_start": None, "weight_end": None,
            "kcal_total": 0, "kcal_days": 0, "protein_total": 0, "alcohol_violations": 0,
            "caffeine_total": 0}
    state_track = {"am_done": 0, "pm_done": 0, "mood_am_sum": 0, "mood_am_n": 0,
                   "sleep_sum": 0, "sleep_n": 0}
    business = {"meetings_product": 0, "meetings_marketing": 0, "meetings_ops": 0,
                "todoist_completed": 0, "todoist_due": 0}
    family = {"family_hours_billed": 0, "vanya_breakfast": 0}

    for d in days:
        # Episodic
        ep = _read(repo / "tracks" / "state" / "episodic" / f"{d.isoformat()}.md")
        if "## AM check-in" in ep and len(ep.split("## AM check-in")[-1].split("##")[0].strip()) > 5:
            state_track["am_done"] += 1
        if "## PM рефлексия" in ep and len(ep.split("## PM рефлексия")[-1].split("##")[0].strip()) > 5:
            state_track["pm_done"] += 1
        # Match formats: "- Утро (1–10): 7", "Состояние: 7", "Утро 7"
        m = re.search(r"-\s*Утро\s*\([^)]*\):\s*(\d{1,2})", ep)
        if not m:
            m = re.search(r"[Сс]остояние:?\s*(\d{1,2})\b(?!\s*[-–])", ep)
        if m:
            try:
                v = int(m.group(1))
                if 1 <= v <= 10:
                    state_track["mood_am_sum"] += v
                    state_track["mood_am_n"] += 1
            except Exception:
                pass
        m = re.search(r"[Сс]он:?\s*(\d+(?:[.,]\d+)?)\s*ч", ep)
        if m:
            try:
                state_track["sleep_sum"] += float(m.group(1).replace(",", "."))
                state_track["sleep_n"] += 1
            except Exception:
                pass

        # Workouts / runs
        if (repo / "tracks" / "body" / "workouts" / f"{d.isoformat()}.md").exists():
            body["strength"] += 1
        if (repo / "tracks" / "body" / "runs" / f"{d.isoformat()}.md").exists():
            body["run"] += 1
        if (repo / "tracks" / "body" / "daily_movement" / f"{d.isoformat()}.md").exists():
            body["core_days"] += 1

        # Food
        food = _read(repo / "tracks" / "body" / "food" / f"{d.isoformat()}.md")
        if food:
            body["kcal_days"] += 1
            m = re.search(r"Калории:?\s*(\d+)", food)
            if m:
                try:
                    body["kcal_total"] += int(m.group(1))
                except Exception:
                    pass
            m = re.search(r"Белок:?\s*([\d.]+)", food)
            if m:
                try:
                    body["protein_total"] += float(m.group(1))
                except Exception:
                    pass
            for cm in re.findall(r"☕\s*(\d+)\s*мг", food):
                try:
                    body["caffeine_total"] += int(cm)
                except Exception:
                    pass
            if re.search(r"\b(пил|выпил|алкогол|вино|пиво|водк)", food, re.IGNORECASE):
                body["alcohol_violations"] += 1

    # Weights — read monthly file
    months = {f"{d.isoformat()[:7]}" for d in days}
    weights = {}
    for m in months:
        wf = _read(repo / "tracks" / "body" / "weights" / f"{m}.md")
        for line in wf.splitlines():
            mt = re.match(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([\d.]+)", line)
            if mt:
                try:
                    weights[mt.group(1)] = float(mt.group(2))
                except Exception:
                    pass
    week_weights = sorted([(d, w) for d, w in weights.items() if start.isoformat() <= d <= end.isoformat()])
    if week_weights:
        body["weight_start"] = week_weights[0][1]
        body["weight_end"] = week_weights[-1][1]

    # Todoist
    try:
        ts = _import_todoist(repo)
        # last 7 days completed
        all_completed = ts.list_completed_today()  # last 24h
        business["todoist_completed_today"] = len(all_completed)
    except Exception:
        pass

    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "days": [d.isoformat() for d in days],
        "body": body,
        "state": state_track,
        "business": business,
        "family": family,
    }


def _format_summary(data: dict) -> str:
    body = data["body"]
    state = data["state"]
    biz = data["business"]

    avg_kcal = body["kcal_total"] / body["kcal_days"] if body["kcal_days"] else 0
    avg_protein = body["protein_total"] / body["kcal_days"] if body["kcal_days"] else 0
    avg_mood = state["mood_am_sum"] / state["mood_am_n"] if state["mood_am_n"] else 0
    avg_sleep = state["sleep_sum"] / state["sleep_n"] if state["sleep_n"] else 0

    weight_delta = ""
    if body["weight_start"] and body["weight_end"]:
        d = body["weight_end"] - body["weight_start"]
        weight_delta = f"{body['weight_start']:.1f} → {body['weight_end']:.1f} ({d:+.1f} кг)"
    else:
        weight_delta = "_нет данных_"

    alc = "✅ 0" if body["alcohol_violations"] == 0 else f"🔴 {body['alcohol_violations']} нарушений"

    lines = [
        f"📊 *Недельный обзор {data['week_start']} → {data['week_end']}*",
        "",
        "*💪 Body*",
        f"  Силовые: {body['strength']}/3",
        f"  Бег: {body['run']}/3",
        f"  Кор: {body['core_days']}/7 дней",
        f"  Вес: {weight_delta}",
        f"  Калории avg: {avg_kcal:.0f} ккал",
        f"  Белок avg: {avg_protein:.0f} г",
        f"  Кофеин неделя: {body['caffeine_total']} мг",
        f"  Алкоголь: {alc}",
        "",
        "*🌱 State*",
        f"  AM: {state['am_done']}/7 · PM: {state['pm_done']}/7",
        f"  Mood avg: {avg_mood:.1f}/10",
        f"  Сон avg: {avg_sleep:.1f} ч",
        "",
        "*💼 Business*",
        f"  Todoist closed (24ч): {biz.get('todoist_completed_today', 0)}",
        f"  _(полная business-блок: clients/meetings — позже когда Attio MCP интегрирован)_",
        "",
    ]
    return "\n".join(lines)


async def send_aggregate(bot: Any, chat_id: int, repo: Path) -> None:
    today = datetime.now(UTC).date()
    data = aggregate(repo, today)
    summary = _format_summary(data)

    # Save aggregate to file
    summaries_dir = repo / "tracks" / "state" / "weekly_summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    out = summaries_dir / f"{data['week_start']}.md"
    out.write_text(
        f"---\ntype: weekly_summary\nweek_start: {data['week_start']}\n"
        f"week_end: {data['week_end']}\ncreated: {datetime.now(UTC).isoformat()}\n---\n\n"
        + summary + "\n\n## Reflection\n\n_(заполнится через 5 вопросов)_\n",
        encoding="utf-8",
    )

    state = _load_state(repo)
    state["weekly_review_active"] = True
    state["weekly_review_data"] = data
    state["weekly_review_step"] = "0"
    state.setdefault("weekly_review_answers", {})
    _save_state(repo, state)

    text = summary + "\n_Открой → tracks/state/weekly_summaries/" + data["week_start"] + ".md_\n\nГотов к рефлексии? 5 вопросов, ~5 мин."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Поехали", callback_data="wr_step:1")],
        [InlineKeyboardButton("Позже", callback_data="wr_step:later")],
        [InlineKeyboardButton("Skip рефлексию", callback_data="wr_step:skip")],
    ])
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


REFLECTION_QUESTIONS = [
    ("wins", "🟢 *3 win недели* — за что благодаришь себя?"),
    ("misses", "🔴 *3 miss* — что не получилось?"),
    ("lesson", "💡 *1 lesson* — что узнал/понял?"),
    ("pattern", "🔁 *1 паттерн* — что заметил повторяющееся?"),
    ("experiment", "🧪 *1 эксперимент* на следующую неделю?"),
]


async def send_question(bot: Any, chat_id: int, repo: Path, idx: int) -> None:
    if idx >= len(REFLECTION_QUESTIONS):
        await send_done(bot, chat_id, repo)
        return
    key, text = REFLECTION_QUESTIONS[idx]
    full = f"*Reflection {idx+1}/5*\n\n{text}\n\n🎙 Голос или текст."

    state = _load_state(repo)
    state["weekly_review_step"] = str(idx + 1)
    _save_state(repo, state)

    await bot.send_message(
        chat_id=chat_id, text=full, parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Голос или текст"),
    )


async def send_done(bot: Any, chat_id: int, repo: Path) -> None:
    state = _load_state(repo)
    answers = state.get("weekly_review_answers", {})
    data = state.get("weekly_review_data", {})

    # Append reflection to file
    if data.get("week_start"):
        out = repo / "tracks" / "state" / "weekly_summaries" / f"{data['week_start']}.md"
        if out.exists():
            content = out.read_text(encoding="utf-8")
            ref_lines = []
            for key, text in REFLECTION_QUESTIONS:
                ans = answers.get(key, "_skip_")
                ref_lines.append(f"### {text.replace('*', '')}\n\n{ans}\n")
            ref_block = "\n".join(ref_lines)
            content = content.replace(
                "## Reflection\n\n_(заполнится через 5 вопросов)_",
                f"## Reflection\n\n{ref_block}",
            )
            out.write_text(content, encoding="utf-8")

    state["weekly_review_active"] = False
    state["weekly_review_step"] = "done"
    state["weekly_review_completed_at"] = datetime.now(UTC).isoformat()
    _save_state(repo, state)

    text = (
        "✅ *Weekly review записан*\n\n"
        f"→ `tracks/state/weekly_summaries/{data.get('week_start')}.md`\n\n"
        "Через 30 минут запускаю planning-week (расставим слоты на след. неделю).\n"
        "_/plan_week — если хочешь раньше._"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("weekly_review done", week_start=data.get("week_start"))


async def handle_wr_callback(update: Any, context: Any, settings: Any = None, **_kwargs: Any) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 1)
    if parts[0] != "wr_step" or len(parts) < 2:
        await query.answer()
        return
    action = parts[1]

    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    bot = query.bot
    chat_id = query.message.chat_id if query.message else None

    if action == "later":
        await query.answer("Позже")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    if action == "skip":
        await query.answer("Skip")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        await send_done(bot, chat_id, repo)
        return

    if action == "1":
        await query.answer("Поехали")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        await send_question(bot, chat_id, repo, 0)
        return

    await query.answer()


async def handle_wr_text_reply(update_or_event: Any, context: Any = None, settings: Any = None, **_kwargs: Any) -> bool:
    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    if not state.get("weekly_review_active"):
        return False
    step = state.get("weekly_review_step", "0")
    try:
        idx = int(step) - 1  # because step is "1" for first question
    except ValueError:
        return False
    if idx < 0 or idx >= len(REFLECTION_QUESTIONS):
        return False

    key = REFLECTION_QUESTIONS[idx][0]
    answers = state.setdefault("weekly_review_answers", {})
    answers[key] = msg.text.strip()
    state["weekly_review_answers"] = answers
    _save_state(repo, state)

    await msg.reply_text("📝 Записано")
    next_idx = idx + 1
    bot = msg.bot
    chat_id = msg.chat_id
    if next_idx >= len(REFLECTION_QUESTIONS):
        await send_done(bot, chat_id, repo)
    else:
        await send_question(bot, chat_id, repo, next_idx)
    return True
