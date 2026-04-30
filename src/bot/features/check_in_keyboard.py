"""Inline-keyboard AM/PM check-in sender.

Bypasses the Sonnet agentic path for genaos:am_check_in / pm_check_in jobs.
Directly builds an InlineKeyboardMarkup and sends via Telegram bot API,
saving cost (no Sonnet call) and giving the user real checkboxes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, List

import structlog
import yaml  # type: ignore[import-untyped]
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

logger = structlog.get_logger()

ROUTINE_ITEMS = [
    ("svet", "☀️", "Свет"),
    ("dvizh", "💪", "Движение"),
    ("hold", "🧊", "Холод"),
    ("meditation", "🧘", "Медитация"),
]


def _state_path(repo: Path) -> Path:
    return repo / "state" / "check_in_state.json"


def _load_state(repo: Path) -> dict:
    from . import _state_io
    return _state_io.load_state(repo)


def _save_state(repo: Path, state: dict) -> None:
    from . import _state_io
    _state_io.save_state(repo, state)


def build_am_routine_keyboard(checked: dict[str, bool]) -> InlineKeyboardMarkup:
    """Build 2x2 grid of routine checkboxes + Готово → button."""
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(ROUTINE_ITEMS), 2):
        row = []
        for slug, emoji, label in ROUTINE_ITEMS[i:i + 2]:
            mark = "✅" if checked.get(slug, False) else "☐"
            row.append(
                InlineKeyboardButton(
                    text=f"{emoji} {label} {mark}",
                    callback_data=f"am_routine:toggle:{slug}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton("Готово →", callback_data="am_routine:done")])
    return InlineKeyboardMarkup(rows)


def _read_yaml(repo: Path) -> dict:
    f = repo / "state" / "protocols" / "check_ins.yaml"
    if not f.exists():
        return {}
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


async def send_am_check_in(bot: Any, chat_id: int, repo: Path) -> None:
    """Build and send AM check-in: routine keyboard + open questions."""
    cfg = _read_yaml(repo)
    am = cfg.get("am_check_in", {})

    state = _load_state(repo)
    # Preserve existing routine_checks if a live (unanswered) AM session
    # was sent in the last 24h — testing should not wipe taps.
    from datetime import timedelta
    prev_sent = state.get("am_sent_at")
    prev_answered = state.get("am_answered", True)
    keep_existing = False
    if prev_sent and not prev_answered:
        try:
            prev_dt = datetime.fromisoformat(prev_sent)
            if datetime.now(UTC) - prev_dt < timedelta(hours=24):
                keep_existing = True
        except Exception:
            pass
    state["am_sent_at"] = datetime.now(UTC).isoformat()
    state["am_answered"] = False
    state["am_locked"] = False
    if not keep_existing or "am_routine_checks" not in state:
        state["am_routine_checks"] = {slug: False for slug, _, _ in ROUTINE_ITEMS}
    _save_state(repo, state)

    # Source of truth: state/protocols/check_ins.yaml → am_check_in.message.
    # The YAML text intentionally omits the routine items (Свет/Движение/
    # Холод/Медитация) — they appear as the inline keyboard below this text.
    # Fallback below covers the case where YAML is missing/broken so the
    # AM ping still goes out.
    text = (am.get("message") or "").strip() or (
        "*☀️ Утренняя рутина* — нажимай галочки что сделал:\n"
        "_(можно тыкать в любом порядке, можно вернуться позже)_\n\n"
        "*🌅 AM check-in* — ответь реплаем на это сообщение:\n\n"
        "1. Состояние (1-10)?\n"
        "2. Часов сна?\n"
        "3. 3 главных дела дня?\n"
        "4. Как проведёшь время с близкими?\n\n"
        "_Можешь ответить одним сообщением целиком или несколько reply._"
    )

    kb = build_am_routine_keyboard(state["am_routine_checks"])
    msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )
    state["am_message_id"] = msg.message_id
    _save_state(repo, state)
    logger.info("AM check-in sent with routine keyboard", chat_id=chat_id, msg=msg.message_id)


async def send_pm_check_in(bot: Any, chat_id: int, repo: Path) -> None:
    """Send PM check-in as plain text — no checkboxes (no morning routine)."""
    cfg = _read_yaml(repo)
    pm = cfg.get("pm_check_in", {})
    text = pm.get("message", "PM check-in").strip()

    state = _load_state(repo)
    state["pm_sent_at"] = datetime.now(UTC).isoformat()
    state["pm_answered"] = False
    state["pm_locked"] = False
    _save_state(repo, state)

    msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    state["pm_message_id"] = msg.message_id
    _save_state(repo, state)
    logger.info("PM check-in sent", chat_id=chat_id, msg=msg.message_id)


async def send_am_plan(bot: Any, chat_id: int, repo: Path) -> None:
    """Send the day's plan in a separate message right after AM check-in.

    Sources:
    - Todoist due_today open tasks (synced into ## План на сегодня section)
    - Manual lines added by check_in_answer middleware

    No Sonnet. Pure Python.
    """
    today = datetime.now(UTC).date().isoformat()
    episodic = repo / "tracks" / "state" / "episodic" / f"{today}.md"

    # Trigger the Todoist→plan sync first so we send fresh data
    try:
        import subprocess, os
        env = os.environ.copy()
        env["GENAOS_REPO_PATH"] = str(repo)
        subprocess.run(
            ["/root/GenaOS/.venv-mcp/bin/python", "/root/GenaOS/scripts/todoist_to_plan.py"],
            env=env, capture_output=True, timeout=20,
        )
    except Exception:
        logger.exception("send_am_plan: todoist sync failed (non-fatal)")

    if not episodic.exists():
        return

    content = episodic.read_text(encoding="utf-8")
    # Extract ## План на сегодня section
    import re
    m = re.search(r"## План на сегодня\s*\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not m:
        return
    body = m.group(1).strip()
    # Drop the todoist marker comments for display
    body = re.sub(r"\s*<!--\s*todoist:[^>]+-->", "", body)
    if not body:
        return

    text = (
        f"📋 *Твой план на сегодня:*\n\n{body}\n\n"
        "_Пиши «сделал [X]» когда закроешь задачу — закрою в Todoist._"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        logger.info("AM plan sent", chat_id=chat_id, items=body.count("\n") + 1)
    except Exception:
        logger.exception("send_am_plan: send failed")
