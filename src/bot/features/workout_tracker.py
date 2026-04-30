"""workout-tracker — 07:30 cron + /workout slash command.

Reads tracks/body/semantic/program.md to know the 12-week cycle.
Reads tracks/state/weekly_plans/<week_start>.md (if exists) to know which
slot today is. Falls back to default Mon-Sun template.

Default schedule (when no weekly_plan):
  Mon → Strength A (light)
  Tue → Run Quality
  Wed → Strength B (medium)
  Thu → Run Easy
  Fri → Strength C (heavy upper)
  Sat → Rest
  Sun → Long Run
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram.constants import ParseMode

logger = structlog.get_logger()


DEFAULT_SCHEDULE = {
    0: ("strength", "A", "Силовая A (light)"),     # Mon
    1: ("run", "quality", "Качество"),
    2: ("strength", "B", "Силовая B (medium)"),
    3: ("run", "easy", "Лёгкий бег"),
    4: ("strength", "C", "Силовая C (heavy upper)"),
    5: ("rest", "rest", "Отдых"),
    6: ("run", "long", "Длинный бег"),             # Sun
}


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _cycle_week(repo: Path, today: date) -> int:
    """Return 1-based week number in the 12-week cycle."""
    program = _read(repo / "tracks" / "body" / "semantic" / "program.md")
    m = re.search(r"cycle_start:\s*(\d{4}-\d{2}-\d{2})", program)
    if not m:
        return 1
    try:
        start = date.fromisoformat(m.group(1))
    except ValueError:
        return 1
    delta = (today - start).days
    week = delta // 7 + 1
    return max(1, min(12, week))


def _week_start_iso(today: date) -> str:
    """Monday of current week."""
    return (today - timedelta(days=today.weekday())).isoformat()


def _weekly_plan_slot(repo: Path, today: date) -> Optional[tuple]:
    """If weekly_plans/<week_start>.md exists, parse today's slot from it.

    Expected table row: | Пн 4.05 | 💪 Силовая | A (light) |
    Returns (kind, key, label) or None.
    """
    wp_dir = repo / "tracks" / "state" / "weekly_plans"
    week_start = _week_start_iso(today)
    f = wp_dir / f"{week_start}.md"
    if not f.exists():
        return None
    text = _read(f)
    weekday_ru = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    abbrev = weekday_ru[today.weekday()]
    for line in text.splitlines():
        if abbrev in line and ("|" in line):
            if "Силовая" in line or "💪" in line:
                m = re.search(r"\b([ABC])\b", line)
                key = m.group(1).upper() if m else "A"
                names = {"A": "Силовая A (light)", "B": "Силовая B (medium)", "C": "Силовая C (heavy upper)"}
                return ("strength", key, names.get(key, f"Силовая {key}"))
            if "Длинный" in line or "🏃🏃" in line:
                return ("run", "long", "Длинный бег")
            if "Качество" in line or "темповой" in line.lower():
                return ("run", "quality", "Качество (темповой/HMP)")
            if "Лёгкий" in line or "easy" in line.lower():
                return ("run", "easy", "Лёгкий бег")
            if "Отдых" in line or "rest" in line.lower():
                return ("rest", "rest", "Отдых")
    return None


def _strength_plan(week: int, key: str) -> str:
    """Strength A/B/C plan based on TB Operator template + cycle week TM ramp."""
    # Training Max increments: +2.5kg every 3 weeks (start week 1)
    tm_bumps = (week - 1) // 3
    tm_squat = 72 + tm_bumps * 2.5
    tm_bench = 54 + tm_bumps * 2.5
    tm_dl = 54 + tm_bumps * 2.5

    if key == "A":  # light
        return (
            f"💪 *Силовая A (light)* — нед.{week}\n\n"
            f"• Присед 3×5 @ 70% TM ≈ {tm_squat * 0.7:.1f} кг\n"
            f"• Жим лёжа 3×5 @ 70% TM ≈ {tm_bench * 0.7:.1f} кг\n"
            f"• Тяга в наклоне 2×10 (~40 кг)\n"
            f"• Кор: планка 3×45с / полое 3×20с\n"
            f"\n_~30 мин. Цель — разгон, не PR._"
        )
    if key == "B":  # medium
        return (
            f"💪 *Силовая B (medium)* — нед.{week}\n\n"
            f"• Жим лёжа 3×5 @ 80% TM ≈ {tm_bench * 0.8:.1f} кг\n"
            f"• Жим стоя 3×8 (легко)\n"
            f"• Подтяг-прогрессия (см. program.md): scap pulls / negatives / inverted rows\n"
            f"• Кор\n"
            f"\n_~35 мин._"
        )
    # C — heavy upper + DL
    return (
        f"💪 *Силовая C (heavy upper + DL)* — нед.{week}\n\n"
        f"• Присед 3×5 @ 80% TM ≈ {tm_squat * 0.8:.1f} кг\n"
        f"  _ИЛИ Становая тяга 3×5 @ 75% TM ≈ {tm_dl * 0.75:.1f} кг (раз в 2 нед)_\n"
        f"• Жим лёжа 1×3 @ 90% TM ≈ {tm_bench * 0.9:.1f} кг (top set)\n"
        f"• Тяга в наклоне 3×8\n"
        f"• Кор\n"
        f"\n_~35 мин. НЕ heavy squat если завтра длинный бег._"
    )


# Higdon Intermediate adapted to 3 run-days
RUN_PLANS = {
    1: {"q": "5 км @ 6:45/км (lazy)", "e": "5 км easy", "l": "8 км @ 6:45/км"},
    2: {"q": "5 км easy + 4×100м strides", "e": "5 км easy", "l": "10 км"},
    3: {"q": "6 км: внутри 3 км @ tempo (5:50/км)", "e": "5 км easy", "l": "11 км"},
    4: {"q": "6 км easy + strides", "e": "5 км easy", "l": "8 км (cutback)"},
    5: {"q": "7 км: внутри 4 км tempo", "e": "6 км easy", "l": "13 км"},
    6: {"q": "8 км: внутри 5 км tempo", "e": "6 км easy", "l": "14 км"},
    7: {"q": "6 км easy + 6×100м strides", "e": "6 км easy", "l": "11 км (cutback)"},
    8: {"q": "8 км: 2×3 км @ HMP (5:40), отдых 3мин", "e": "6 км easy", "l": "16 км"},
    9: {"q": "9 км: 6 км tempo", "e": "7 км easy", "l": "17 км"},
    10: {"q": "8 км: 5 км @ HMP", "e": "6 км easy", "l": "14 км (cutback)"},
    11: {"q": "8 км: 3 км @ HMP + 2 км tempo", "e": "5 км easy", "l": "18 км (peak!)"},
    12: {"q": "5 км easy + strides (taper)", "e": "4 км easy + strides", "l": "🏁 RACE 21.1 км"},
}


def _run_plan(week: int, key: str) -> str:
    plan = RUN_PLANS.get(week, RUN_PLANS[1])
    if key == "long":
        return (
            f"🏃🏃 *Длинный бег* — нед.{week}\n\n"
            f"• {plan['l']}\n"
            f"• Темп: easy 6:30-7:00/км (фокус на time-on-feet)\n\n"
            f"_⚠️ Якорь программы. НЕ пропускать._"
        )
    if key == "quality":
        return (
            f"🏃 *Качественный бег* — нед.{week}\n\n"
            f"• {plan['q']}\n\n"
            f"_Темповой 5:40-5:55/км · HMP 5:30-5:55/км_"
        )
    # easy
    return (
        f"🏃 *Лёгкий бег* — нед.{week}\n\n"
        f"• {plan['e']}\n"
        f"• Темп easy 6:30-7:00/км\n\n"
        f"_Должен быть способен говорить._"
    )


def build_today_plan(repo: Path, today: Optional[date] = None) -> str:
    """Generate today's workout plan as Markdown string."""
    today = today or datetime.now(UTC).date()
    week = _cycle_week(repo, today)
    slot = _weekly_plan_slot(repo, today) or DEFAULT_SCHEDULE[today.weekday()]
    kind, key, label = slot

    if kind == "strength":
        return _strength_plan(week, key)
    if kind == "run":
        return _run_plan(week, key)
    return (
        f"😴 *Отдых* — нед.{week}\n\n"
        f"Можно прогулка 30 мин или mobility / стретчинг.\n"
        f"_Завтра {DEFAULT_SCHEDULE[(today.weekday() + 1) % 7][2]}._"
    )


async def send_workout_today(bot: Any, chat_id: int, repo: Path) -> None:
    """07:30 cron — send today's plan."""
    today = datetime.now(UTC).date()
    plan = build_today_plan(repo, today)

    footer = (
        "\n\n"
        "_/done — записать как сделано · /skip <причина> · /workout swap <упр> <замена>_"
    )

    text = plan + footer
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("workout-tracker plan sent", date=today.isoformat())


async def send_workout_now_command(bot: Any, chat_id: int, repo: Path) -> None:
    """/workout slash — show today's plan on demand."""
    await send_workout_today(bot, chat_id, repo)
