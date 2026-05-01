"""Core stack — ежедневная утренняя физо: планка, отжимания, hollow.

Программа определена в tracks/body/semantic/program.md (cycle 12 нед).

Slash: /core <plank> <push> <hollow>
       /core              — ForceReply prompt
       /core 60 25 45     — записать
       /core done         — просто отметить (без чисел) → файл создаётся
                            пустой, streak засчитывается

File: tracks/body/daily_movement/<date>.md (compute_streaks читает его).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import structlog
from telegram import ForceReply

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


# Cycle goals from tracks/body/semantic/program.md
CYCLE_START = date(2026, 5, 4)
GOALS_BY_WEEK: List[Tuple[int, dict]] = [
    (3,  {"plank": 45, "side": 30, "push": "2×15", "hollow": 20, "label": "нед.1-3"}),
    (7,  {"plank": 60, "side": 45, "push": "2×20", "hollow": 30, "label": "нед.4-7"}),
    (11, {"plank": 90, "side": 60, "push": "3×20", "hollow": 45, "label": "нед.8-11"}),
    (99, {"plank": 120, "side": 90, "push": "3×25", "hollow": 60, "label": "нед.12"}),
]


def _local_today() -> date:
    return datetime.now(UTC).astimezone(_LOCAL_TZ).date()


def _cycle_week(today: date) -> int:
    delta = (today - CYCLE_START).days
    if delta < 0:
        return 0  # pre-cycle
    return delta // 7 + 1


def _goals_for(week: int) -> dict:
    week = max(1, week)
    for max_w, g in GOALS_BY_WEEK:
        if week <= max_w:
            return g
    return GOALS_BY_WEEK[-1][1]


def _file_path(repo: Path, today: date) -> Path:
    return repo / "tracks" / "body" / "daily_movement" / f"{today.isoformat()}.md"


def _format_status(done: dict, goals: dict) -> str:
    """Build markdown body comparing done vs goals."""
    lines = []
    if "plank" in done:
        d = done["plank"]
        target = goals["plank"]
        emoji = "✅" if d >= target else f"🟡 -{target-d}с"
        lines.append(f"- Планка: **{d}с** (цель {target}с) {emoji}")
    if "push" in done:
        d = done["push"]
        lines.append(f"- Отжимания: **{d}** (цель {goals['push']})")
    if "hollow" in done:
        d = done["hollow"]
        target = goals["hollow"]
        emoji = "✅" if d >= target else f"🟡 -{target-d}с"
        lines.append(f"- Hollow body: **{d}с** (цель {target}с) {emoji}")
    return "\n".join(lines) if lines else "_(только отметка, без цифр)_"


def _build_file(today: date, week: int, goals: dict, done: dict, note: Optional[str]) -> str:
    parts = [
        "---",
        "type: episodic",
        "track: body",
        "subtype: daily_movement",
        f"date: {today.isoformat()}",
        f"cycle_week: {week}",
        "---",
        "",
        f"# Кор-стек {today.isoformat()} (нед.{week or 0} — {goals['label']})",
        "",
        "## Цели",
        f"- Планка передняя: {goals['plank']} сек",
        f"- Боковая планка: {goals['side']} сек × 2",
        f"- Отжимания: {goals['push']}",
        f"- Hollow body: {goals['hollow']} сек",
        "",
        "## Сделано",
        _format_status(done, goals),
        "",
    ]
    if note:
        parts += ["## Заметка", note, ""]
    return "\n".join(parts)


async def handle_core_command(bot: Any, chat_id: int, repo: Path, args: List[str]) -> None:
    """Slash /core entry. args = [] | ['done'] | [n1, n2, n3] | [n1, n2, n3, ...note]."""
    today = _local_today()
    week = _cycle_week(today)
    goals = _goals_for(week)

    # Empty args — ForceReply prompt
    if not args:
        text = (
            f"💪 Кор-стек {today.isoformat()} (нед.{week} — {goals['label']})\n\n"
            f"Цели сегодня:\n"
            f"  Планка {goals['plank']}с · Боковая {goals['side']}с×2 · "
            f"Отжим {goals['push']} · Hollow {goals['hollow']}с\n\n"
            "Введи 3 числа: планка_сек отжим hollow_сек\n"
            "Например: `60 25 30`\n"
            "Или /core done — просто галочка."
        )
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=ForceReply(selective=False, input_field_placeholder="60 25 30"),
        )
        return

    # /core done → just create file with no numbers
    if args[0].lower() in ("done", "ok", "ok!"):
        f = _file_path(repo, today)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_build_file(today, week, goals, {}, None), encoding="utf-8")
        await bot.send_message(
            chat_id=chat_id,
            text=f"💪 Кор {today.isoformat()} — отмечено (без цифр). Streak +1.",
        )
        return

    # Try parse 3 numbers
    nums: List[int] = []
    note_parts: List[str] = []
    for a in args:
        if a.isdigit() and len(nums) < 3:
            nums.append(int(a))
        else:
            note_parts.append(a)
    if len(nums) < 1:
        await bot.send_message(
            chat_id=chat_id,
            text="💪 Нужны цифры. Например: `/core 60 25 30` (план/отж/hollow) или `/core done`.",
            parse_mode="Markdown",
        )
        return

    done: dict = {}
    if len(nums) >= 1:
        done["plank"] = nums[0]
    if len(nums) >= 2:
        done["push"] = nums[1]
    if len(nums) >= 3:
        done["hollow"] = nums[2]
    note = " ".join(note_parts).strip() or None

    f = _file_path(repo, today)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_build_file(today, week, goals, done, note), encoding="utf-8")

    status = _format_status(done, goals)
    await bot.send_message(
        chat_id=chat_id,
        text=f"💪 Кор {today.isoformat()} (нед.{week}):\n\n{status}\n\nStreak +1.",
        parse_mode="Markdown",
    )
    logger.info("core done", date=today.isoformat(), week=week, done=done)


async def handle_core_reply(
    update_or_event: Any, context: Any = None, settings: Any = None, **_kwargs: Any
) -> bool:
    """Capture ForceReply to /core prompt — parse 3 numbers, write file."""
    settings = settings or (context.bot_data.get("settings") if context else None)
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")

    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False

    reply = getattr(msg, "reply_to_message", None)
    if reply is None:
        return False
    text = (reply.text or "").strip()
    # Only intercept replies to /core ForceReply prompts
    if not (text.startswith("💪 Кор-стек") and "Введи 3 числа" in text):
        return False

    parts = msg.text.replace(",", " ").split()
    args = [p for p in parts if p]
    bot = msg.get_bot()
    chat_id = msg.chat_id
    await handle_core_command(bot, chat_id, repo, args)
    return True
