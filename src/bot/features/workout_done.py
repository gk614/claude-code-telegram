"""Workout /done handler — fixate today's training session.

Writes tracks/body/workouts/<today>.md (strength) or tracks/body/runs/<today>.md (run)
based on what the schedule says for today.

Optional argument: free-text comment OR structured weights ("80/55/55" -> squat/bench/row).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram.constants import ParseMode

logger = structlog.get_logger()


def _today_slot(repo: Path, today: date) -> tuple[str, str, str]:
    """Determine today's slot via workout_tracker logic. Returns (kind, key, label)."""
    from .workout_tracker import _weekly_plan_slot, DEFAULT_SCHEDULE
    return _weekly_plan_slot(repo, today) or DEFAULT_SCHEDULE[today.weekday()]


def _ensure_file(target: Path, kind: str, today: date) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target.read_text(encoding="utf-8")
    section = "## Сессия" if kind == "strength" else "## Бег"
    text = (
        f"---\ndate: {today.isoformat()}\ntrack: body\nsubtype: {kind}\n---\n\n"
        f"# {kind.title()} — {today.isoformat()}\n\n"
        f"{section}\n"
    )
    target.write_text(text, encoding="utf-8")
    return text


async def handle_done_command(bot: Any, chat_id: int, repo: Path, comment: Optional[str] = None) -> None:
    today = datetime.now(UTC).date()
    kind, key, label = _today_slot(repo, today)

    if kind == "rest":
        await bot.send_message(
            chat_id=chat_id,
            text="😴 Сегодня rest по плану. Зачем `/done`? \n_Если делал что-то — напиши: «утренний кор сделал»._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    sub_dir = "workouts" if kind == "strength" else "runs"
    target = repo / "tracks" / "body" / sub_dir / f"{today.isoformat()}.md"
    content = _ensure_file(target, kind, today)

    hhmm = datetime.now(UTC).astimezone().strftime("%H:%M")
    note = f" — {comment.strip()}" if comment else ""
    line = f"\n- **{hhmm}:** ✅ DONE — {label}{note}\n"

    with target.open("a", encoding="utf-8") as f:
        f.write(line)

    emoji = "💪" if kind == "strength" else "🏃"
    ack = (
        f"{emoji} *DONE* — {label}\n"
        f"→ `tracks/body/{sub_dir}/{today.isoformat()}.md`\n"
        f"_Reward gate в 22:30 учтёт ✅_"
    )
    if comment:
        ack += f"\n\n_Заметка: {comment.strip()}_"

    await bot.send_message(chat_id=chat_id, text=ack, parse_mode=ParseMode.MARKDOWN)
    logger.info("workout done", kind=kind, key=key, has_comment=bool(comment))


async def handle_skip_command(bot: Any, chat_id: int, repo: Path, reason: str) -> None:
    today = datetime.now(UTC).date()
    kind, key, label = _today_slot(repo, today)

    if kind == "rest":
        await bot.send_message(chat_id=chat_id, text="😴 Сегодня и так rest.", parse_mode=ParseMode.MARKDOWN)
        return

    sub_dir = "workouts" if kind == "strength" else "runs"
    target = repo / "tracks" / "body" / sub_dir / f"{today.isoformat()}.md"
    _ensure_file(target, kind, today)

    hhmm = datetime.now(UTC).astimezone().strftime("%H:%M")
    line = f"\n- **{hhmm}:** ❌ SKIP — {label} (reason: {reason})\n"
    with target.open("a", encoding="utf-8") as f:
        f.write(line)

    await bot.send_message(
        chat_id=chat_id,
        text=f"⚠️ *SKIP* — {label} ({reason})\n_Это факт. На weekly разберём паттерн._",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("workout skip", kind=kind, key=key, reason=reason)
