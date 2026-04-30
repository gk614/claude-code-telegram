"""food-tracker alerts — 21:00 cron checks daily totals vs nutrition_plan.md goals.

Reads tracks/body/food/<today>.md and tracks/body/semantic/nutrition_plan.md.
Sends gentle reminder if protein/calories/caffeine miss targets.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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


def _parse_goals(plan_text: str) -> dict:
    """Extract goals from nutrition_plan.md."""
    goals = {"kcal": 2500, "protein": 160, "fat": 80, "carbs": 280, "caffeine_max": 400, "sodium_max": 2300}
    m = re.search(r"Калории:?\s*\|\s*(\d+)", plan_text)
    if m:
        goals["kcal"] = int(m.group(1))
    m = re.search(r"Белок\*?\*?:?\s*\|\s*\*?\*?(\d+)", plan_text)
    if m:
        goals["protein"] = int(m.group(1))
    return goals


def _parse_today_totals(food_text: str) -> dict:
    totals = {"kcal": 0, "protein": 0.0, "caffeine": 0, "alcohol": False}
    m = re.search(r"Калории:?\s*(\d+)", food_text)
    if m:
        totals["kcal"] = int(m.group(1))
    m = re.search(r"Белок:?\s*([\d.]+)", food_text)
    if m:
        totals["protein"] = float(m.group(1))
    for cm in re.findall(r"☕\s*(\d+)\s*мг", food_text):
        try:
            totals["caffeine"] += int(cm)
        except Exception:
            pass
    if re.search(r"\b(пил|выпил|алкогол|вино|пиво|водк)", food_text, re.IGNORECASE):
        totals["alcohol"] = True
    return totals


async def send_food_evening_alert(bot: Any, chat_id: int, repo: Path) -> None:
    """21:00 — check today's food vs goals, send alert if anything off."""
    today = datetime.now(UTC).date().isoformat()
    food_text = _read(repo / "tracks" / "body" / "food" / f"{today}.md")
    plan_text = _read(repo / "tracks" / "body" / "semantic" / "nutrition_plan.md")

    if not food_text:
        # Гена ничего не записал в еду — это сам по себе alert
        await bot.send_message(
            chat_id=chat_id,
            text="🍽 *21:00 — еды за сегодня нет в логах*\n\nЗапиши хоть кратко что ел — для трекинга.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    goals = _parse_goals(plan_text)
    totals = _parse_today_totals(food_text)
    today_d = datetime.now(UTC).date()

    alerts = []
    suggestions = []

    # Protein check
    if totals["protein"] < 130:
        deficit = goals["protein"] - totals["protein"]
        alerts.append(f"💪 Белок: {totals['protein']:.0f}/{goals['protein']} г — недобор {deficit:.0f} г")
        suggestions.extend([
            "  • Творог 200 г = +36 г белка",
            "  • Whey shake = +24 г",
            "  • 3 яйца = +18 г",
        ])

    # Calories check
    if totals["kcal"] > goals["kcal"] + 200:
        alerts.append(f"🍽 Калории: {totals['kcal']}/{goals['kcal']} ккал — на {totals['kcal'] - goals['kcal']} выше нормы")

    # Caffeine
    if today_d >= COLD_TURKEY_CAFFEINE:
        if totals["caffeine"] > 0:
            alerts.append(f"🔴 Кофеин: {totals['caffeine']} мг (cold turkey с 15.05 — должен быть 0)")
    else:
        if totals["caffeine"] > goals["caffeine_max"]:
            alerts.append(f"☕ Кофеин: {totals['caffeine']}/{goals['caffeine_max']} мг — выше нормы")

    if not alerts and not totals["alcohol"]:
        return  # all green, no alert

    parts = ["🟡 *Вечерняя сверка по еде:*\n"]
    parts.extend(alerts)
    if suggestions:
        parts.append("\n_До сна добавишь?_")
        parts.extend(suggestions)
    if totals["alcohol"]:
        parts.append("\n🔴 *Алкоголь зафиксирован* — hard non-negotiable. На weekly разберём.")

    await bot.send_message(chat_id=chat_id, text="\n".join(parts), parse_mode=ParseMode.MARKDOWN)
    logger.info("food_alert sent", alerts_count=len(alerts), alcohol=totals["alcohol"])
