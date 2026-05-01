"""reward-gate — 22:30 first + 23:00 final accountability checkpoint.

Checks 5-6 conditions and reports passed/near_miss/not_passed.
NOT punitive — informational mirror only.

Conditions:
  1. AM check-in закрыт
  2. PM check-in закрыт (only at final 23:00)
  3. 3 ключевые задачи закрыты в Todoist (or all open)
  4. Hard non-negotiables (alcohol_zero, meditation done, caffeine_zero post 2026-05-15)
  5. Тренировка done (if was a slot today)
  6. Биллинг passed (TODO — needs time-billing)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram.constants import ParseMode

logger = structlog.get_logger()

COLD_TURKEY_CAFFEINE = date(2026, 5, 15)


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _state(repo: Path, name: str) -> dict:
    f = repo / "state" / name
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _import_todoist(repo: Path):
    scripts_path = str(repo / "scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    import todoist_sync  # type: ignore
    return todoist_sync


def _evaluate(repo: Path, today: date, *, final: bool) -> dict:
    """Evaluate gate conditions. Returns {conditions: {name: bool|None}, missed: [str], score: 'passed'|'near_miss'|'not_passed'}"""
    cis = _state(repo, "check_in_state.json")
    streaks = _state(repo, "streaks.json")
    today_status = streaks.get("today_status", {})
    today_iso = today.isoformat()

    conditions = {}
    missed = []

    # 1. AM
    am_done = today_status.get("am_checkin", False) or bool(cis.get("am_answered"))
    conditions["am_checkin"] = am_done
    if not am_done:
        missed.append("☀️ AM check-in не закрыт")

    # 2. PM (only at final)
    if final:
        pm_done = today_status.get("pm_checkin", False) or bool(cis.get("pm_answered"))
        conditions["pm_checkin"] = pm_done
        if not pm_done:
            missed.append("🌙 PM check-in не закрыт")

    # 3. 3 ключевые задачи в Todoist
    # B-P1-1 day rollover: stale key_tasks from yesterday confuse counts.
    today_iso = today.isoformat()
    if cis.get("key_tasks_date") != today_iso:
        key_tasks = []  # not selected today
    else:
        key_tasks = cis.get("key_tasks_today", [])
    if key_tasks:
        try:
            ts = _import_todoist(repo)
            completed = ts.list_completed_today()
            completed_texts = {t["content"].lower().strip() for t in completed}
            done_count = sum(1 for kt in key_tasks if kt.lower().strip() in completed_texts)
            conditions["key_tasks"] = done_count >= len(key_tasks)
            if done_count < len(key_tasks):
                missed.append(f"⭐ Ключевые: {done_count}/{len(key_tasks)} закрыто в Todoist")
        except Exception:
            conditions["key_tasks"] = None
            logger.exception("reward_gate: todoist check failed")
    else:
        # No /key today — gate failure (motivates daily prioritization)
        conditions["key_tasks"] = False
        missed.append("⭐ Ключевые задачи не выбраны (используй /key утром)")

    # 4. Hard non-negotiables
    alcohol_zero = today_status.get("alcohol_zero", True)  # presumption: not drunk if no data
    meditation_done = today_status.get("morning_meditation", False)
    conditions["alcohol_zero"] = alcohol_zero
    conditions["meditation"] = meditation_done
    if not alcohol_zero:
        missed.append("🚫 Алкоголь зафиксирован (hard non-negotiable)")
    if not meditation_done:
        missed.append("🧘 Утренняя медитация не отмечена")

    # Caffeine zero only after cold turkey date
    if today >= COLD_TURKEY_CAFFEINE:
        caf_zero = today_status.get("caffeine_zero", True)
        conditions["caffeine_zero"] = caf_zero
        if not caf_zero:
            missed.append("☕ Кофеин зафиксирован (cold turkey с 15.05)")

    # 5. Тренировка (if was a slot)
    weekday = today.weekday()
    # Default schedule: 0=Mon→strength, 1=run, 2=strength, 3=run, 4=strength, 5=rest, 6=run
    has_workout_slot = weekday in (0, 1, 2, 3, 4, 6)
    if has_workout_slot:
        # Check if either workouts/<today>.md or runs/<today>.md exists
        wo = repo / "tracks" / "body" / "workouts" / f"{today_iso}.md"
        rn = repo / "tracks" / "body" / "runs" / f"{today_iso}.md"
        workout_done = wo.exists() or rn.exists()
        conditions["workout"] = workout_done
        if not workout_done:
            missed.append("💪 Тренировка не зафиксирована")

    # Core stack — daily streak target
    core_f = repo / "tracks" / "body" / "daily_movement" / f"{today_iso}.md"
    if core_f.exists():
        conditions["core_stack"] = True
    else:
        conditions["core_stack"] = False
        missed.append("🌱 Кор-стек не отмечен")

    # Time-billing coverage (≥80% from billing_aggregate at 22:30)
    cov = cis.get("billing_coverage")
    if isinstance(cov, (int, float)):
        conditions["billing_coverage"] = cov >= 0.8
        if not conditions["billing_coverage"]:
            missed.append(f"📊 Биллинг coverage {cov:.0%} (<80%)")

    # Score
    hard_failures = [k for k, v in conditions.items() if v is False]
    if not hard_failures:
        score = "passed"
    elif len(hard_failures) == 1:
        score = "near_miss"
    else:
        score = "not_passed"

    # Alcohol always taints (special case)
    if conditions.get("alcohol_zero") is False:
        score = "alcohol_taint"

    return {
        "conditions": conditions,
        "missed": missed,
        "score": score,
        "hard_failures": hard_failures,
    }


def _format_status(conditions: dict) -> str:
    lines = []
    if "am_checkin" in conditions:
        lines.append(f"☀️ AM: {'✅' if conditions['am_checkin'] else '❌'}")
    if "pm_checkin" in conditions:
        lines.append(f"🌙 PM: {'✅' if conditions['pm_checkin'] else '❌'}")
    kt = conditions.get("key_tasks")
    if kt is True:
        lines.append("⭐ Ключевые: всё ✅")
    elif kt is False:
        lines.append("⭐ Ключевые: 🟡 не все")
    elif kt is None:
        lines.append("⭐ Ключевые: _не выбраны_")
    lines.append(f"🚫 Алкоголь: {'✅ 0' if conditions.get('alcohol_zero') else '🔴 нарушен'}")
    lines.append(f"🧘 Медитация: {'✅' if conditions.get('meditation') else '❌'}")
    if "caffeine_zero" in conditions:
        lines.append(f"☕ Кофеин: {'✅ 0' if conditions['caffeine_zero'] else '🔴 нарушен'}")
    if "core_stack" in conditions:
        lines.append(f"🌱 Кор: {'✅' if conditions['core_stack'] else '❌'}")
    if "billing_coverage" in conditions:
        lines.append(f"📊 Биллинг: {'✅' if conditions['billing_coverage'] else '🟡'}")
    if "workout" in conditions:
        lines.append(f"💪 Тренировка: {'✅' if conditions['workout'] else '⏳'}")
    return "\n".join(f"   {l}" for l in lines)


async def send_first_gate(bot: Any, chat_id: int, repo: Path) -> None:
    """22:30 — preview before/after PM, no PM check yet."""
    today = datetime.now(UTC).date()
    result = _evaluate(repo, today, final=False)
    score = result["score"]
    cond_text = _format_status(result["conditions"])

    if score == "passed":
        text = (
            f"🟢 *Перед PM — всё на месте:*\n\n{cond_text}\n\n"
            f"_PM в 22:00 — последний шаг._"
        )
    elif score == "near_miss":
        text = (
            f"🟡 *Перед PM — почти всё:*\n\n{cond_text}\n\n"
            f"⏳ _Осталось добить до 23:00 если хочешь чистый gate._"
        )
    else:
        text = (
            f"🟡 *Перед PM — статус:*\n\n{cond_text}\n\n"
            f"_До конца дня ~1 час. Что-то ещё?_"
        )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("reward_gate first sent", score=score)


async def send_final_gate(bot: Any, chat_id: int, repo: Path) -> None:
    """23:00 — final после PM."""
    today = datetime.now(UTC).date()
    result = _evaluate(repo, today, final=True)
    score = result["score"]
    cond_text = _format_status(result["conditions"])

    if score == "passed":
        # Streak check
        streaks = _state(repo, "streaks.json")
        gate_streak = streaks.get("habits", {}).get("gate_passed", {}).get("current_streak", 0)
        text = (
            f"🟢 *Прошёл gate. Заслуженный вечер.*\n\n{cond_text}\n"
            + (f"\n🔥 Streak passed-gate: {gate_streak} дней" if gate_streak >= 2 else "")
        )
    elif score == "near_miss":
        miss = result["missed"][0] if result["missed"] else "1 пункт"
        text = (
            f"🟡 *Gate почти прошёл.* Один открытый пункт:\n{miss}\n\n{cond_text}\n\n"
            f"_Это факт, не оценка._"
        )
    elif score == "alcohol_taint":
        text = (
            f"🔴 *Gate с тёмным пятном.*\n"
            f"Алкоголь зафиксирован — hard non-negotiable.\n"
            f"Остальное может быть зелёным:\n\n{cond_text}\n\n"
            f"_Это первый пункт на завтрашнем разборе. Кому хочешь рассказать?_"
        )
    else:
        text = (
            f"🔴 *Gate не пройден сегодня.*\n\n{cond_text}\n\n"
            f"_Это факт, не оценка. На weekly review разберём паттерн._"
        )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("reward_gate final sent", score=score)


async def send_gate_command(bot: Any, chat_id: int, repo: Path) -> None:
    """/gate — show current status anytime."""
    today = datetime.now(UTC).date()
    result = _evaluate(repo, today, final=False)
    cond_text = _format_status(result["conditions"])
    now = datetime.now(UTC).strftime("%H:%M UTC")
    text = (
        f"📋 *Gate-статус на {now}*\n\n{cond_text}\n\n"
        f"_Финальный gate в 23:00 (15:00 UTC) — после PM._"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
