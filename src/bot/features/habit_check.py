"""habit-check 21:00 / 21:05 alerts (deterministic Python, no LLM).

Reads state/streaks.json and tracks/state/episodic/<today>.md to determine
which non-negotiables aren't ticked today. Sends:

- 21:00: 🟡 soft alert if anything missed today (1-day miss)
- 21:05: 🔴 red alert if any habit has consecutive_misses >= 2

Replaces the Sonnet-prompt-based non_negotiables_monitor / never_miss_twice
slots with direct Python dispatchers (cheaper, deterministic, no LLM cost).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telegram.constants import ParseMode

logger = structlog.get_logger()


def _streaks_path(repo: Path) -> Path:
    return repo / "state" / "streaks.json"


def _refresh_streaks(repo: Path) -> dict:
    """Run scripts/compute_streaks.py and return fresh streaks.json."""
    try:
        env = os.environ.copy()
        env["GENAOS_REPO_PATH"] = str(repo)
        # Use repo's MCP venv if exists, else fall back to system python
        py = "/root/GenaOS/.venv-mcp/bin/python"
        if not Path(py).exists():
            py = sys.executable
        subprocess.run(
            [py, str(repo / "scripts" / "compute_streaks.py")],
            env=env, capture_output=True, timeout=30, check=False,
        )
    except Exception:
        logger.exception("habit_check: compute_streaks failed (non-fatal)")
    f = _streaks_path(repo)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


HABIT_RU_NAMES = {
    "alcohol_zero": "🚫 Алкоголь = 0",
    "caffeine_zero": "☕ Кофеин = 0",
    "morning_meditation": "🧘 Утренняя медитация",
    "am_checkin": "☀️ AM check-in",
    "pm_checkin": "🌙 PM check-in",
    "sleep_7plus": "😴 Сон ≥ 7ч",
    "morning_weight": "📊 Утренний вес",
    "core_stack": "🌱 Кор-стек",
}


async def send_non_negotiables_alert(bot: Any, chat_id: int, repo: Path) -> None:
    """21:00 — soft alert if today has any missed non-negotiable."""
    data = _refresh_streaks(repo)
    today_status = data.get("today_status", {})

    # Check today's hard non-negotiables (alcohol, meditation, AM, PM presumably tonight)
    missed = []
    if today_status.get("alcohol_zero") is False:
        missed.append("🚫 Алкоголь нарушен")
    if not today_status.get("morning_meditation"):
        missed.append("🧘 Медитация")
    if not today_status.get("am_checkin"):
        missed.append("☀️ AM check-in")
    # PM is sent at 22:00 so we don't alert on PM at 21:00

    if not missed:
        logger.info("non_negotiables: all green, no alert")
        return

    text = (
        "🟡 *Сегодня не отмечено:*\n"
        + "\n".join(f"  • {m}" for m in missed)
        + "\n\n_Есть время сделать сейчас?_"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("non_negotiables alert sent", missed=missed)


async def send_never_miss_twice_alert(bot: Any, chat_id: int, repo: Path) -> None:
    """21:05 — red alert if any habit has consecutive_misses >= 2."""
    data = _refresh_streaks(repo)
    habits = data.get("habits", {})

    red_alerts = []
    for habit_key, name in HABIT_RU_NAMES.items():
        h = habits.get(habit_key, {})
        misses = h.get("consecutive_misses", 0)
        if misses >= 2:
            red_alerts.append((name, misses))

    if not red_alerts:
        return

    parts = ["🔴 *Never miss twice — патерн обнаружен:*\n"]
    for name, misses in red_alerts:
        parts.append(f"  • {name}: {misses} дня подряд")
    parts.append("\n_Не «ты должен» — просто помогаю заметить. Что мешает?_")
    text = "\n".join(parts)

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("never_miss_twice alert sent", count=len(red_alerts))


async def update_streaks_after_pm(bot: Any, chat_id: int, repo: Path) -> None:
    """23:30 — recompute streaks after PM is closed. Optionally celebrate milestones."""
    data = _refresh_streaks(repo)
    habits = data.get("habits", {})

    celebrations = []
    for habit_key, name in HABIT_RU_NAMES.items():
        h = habits.get(habit_key, {})
        cur = h.get("current_streak", 0)
        if cur > 7 and cur % 7 == 0:
            celebrations.append((name, cur))
        # New record
        longest = h.get("longest_streak", 0)
        if cur > 0 and cur == longest and cur > 1:
            # Don't double-celebrate (no easy way to know if we already celebrated)
            pass

    if celebrations:
        parts = []
        for name, cur in celebrations:
            parts.append(f"🔥 *{cur} дней подряд* — {name}")
        text = "\n".join(parts)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        logger.info("streak celebration", celebrations=celebrations)


async def send_streaks_command(bot: Any, chat_id: int, repo: Path) -> None:
    """/streaks slash command — show current streaks table."""
    data = _refresh_streaks(repo)
    habits = data.get("habits", {})
    today = data.get("today", "—")

    lines = [f"🔥 *Streak'и на {today}*\n"]
    for habit_key, name in HABIT_RU_NAMES.items():
        h = habits.get(habit_key, {})
        cur = h.get("current_streak", 0)
        longest = h.get("longest_streak", 0)
        emoji = "✅" if cur >= 1 else ("⏳" if cur == 0 and h.get("last_miss_date") is None else "")
        lines.append(f"  {name}: *{cur}* дней (рекорд {longest}) {emoji}")

    bedtime = habits.get("consistent_bedtime", {})
    if bedtime.get("rolling_5d_variance_min") is not None:
        v = bedtime["rolling_5d_variance_min"]
        emoji = "✅" if bedtime.get("compliant") else "🟡"
        lines.append(f"  🌙 Постоянство сна (5д): *±{v} мин* {emoji}")

    text = "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
