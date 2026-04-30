"""Task review pre-PM (21:30 cron).

Reads Todoist due_today + completed_today, shows status to Гена,
parses reply "1, 3, 5" → close_task() for each.

Flow:
  21:30 cron → send_task_review() shows closed/open lists with numbered open
  Гена replies "1, 3, 5" or /skip
  middleware (check_in_answer.py early-check) routes to handle_task_review_reply
  → todoist_sync.close_task(id) for each → ack
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telegram import ForceReply
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


def _import_todoist_sync(repo: Path):
    scripts_path = str(repo / "scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    import todoist_sync  # type: ignore
    return todoist_sync


async def send_task_review(bot: Any, chat_id: int, repo: Path) -> None:
    """Pre-PM 21:30 — Todoist daily review."""
    try:
        ts = _import_todoist_sync(repo)
        open_tasks = ts.list_due_today()
        completed = ts.list_completed_today()
    except Exception:
        logger.exception("task_review: todoist sync failed")
        await bot.send_message(
            chat_id=chat_id,
            text="📋 Сверка задач — _Todoist недоступен сейчас. PM в 22:00 как обычно._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    open_ids = []
    open_lines = []
    for i, t in enumerate(open_tasks, 1):
        open_ids.append(t["id"])
        overdue = ""
        if t.get("due_date"):
            try:
                due = datetime.fromisoformat(t["due_date"]).date()
                today = datetime.now(UTC).date()
                if due < today:
                    overdue = f" ⚠️{(today - due).days}д"
            except Exception:
                pass
        open_lines.append(f"{i}. {t['content']}{overdue}")

    closed_lines = [f"  ✅ {t['content']}" for t in completed[:10]]

    msg_lines = [f"📋 *Сверка задач за день*\n"]
    if completed:
        msg_lines.append(f"✅ *Закрыто в Todoist ({len(completed)}):*")
        msg_lines.extend(closed_lines)
        if len(completed) > 10:
            msg_lines.append(f"  _… ещё {len(completed) - 10}_")
        msg_lines.append("")

    if open_tasks:
        msg_lines.append(f"🟡 *Не закрыто ({len(open_tasks)}):*")
        msg_lines.extend(open_lines)
        msg_lines.append("")
        msg_lines.append(
            "_Что забыл закрыть в Todoist?_\n"
            "Перечисли номера через запятую: «1, 3, 5» или `/skip`"
        )
    else:
        msg_lines.append("🎉 _Все задачи закрыты._")

    text = "\n".join(msg_lines)

    state = _load_state(repo)
    state["task_review_open_ids"] = open_ids
    state["task_review_active"] = True
    state["task_review_sent_at"] = datetime.now(UTC).isoformat()
    _save_state(repo, state)

    if open_tasks:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ForceReply(selective=False, input_field_placeholder="«1, 3, 5» или /skip"),
        )
    else:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        state = _load_state(repo)
        state["task_review_active"] = False
        _save_state(repo, state)
    logger.info("task_review sent", open_count=len(open_tasks), closed=len(completed))


async def handle_task_review_reply(
    update_or_event: Any,
    context: Any = None,
    settings: Any = None,
    **_kwargs: Any,
) -> bool:
    """Process plain-text reply during active task_review.

    Returns True if message consumed.
    """
    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False

    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    if not state.get("task_review_active"):
        return False

    text = msg.text.strip()
    if text.lower() in ("/skip", "skip"):
        state["task_review_active"] = False
        _save_state(repo, state)
        await msg.reply_text("📋 Skip — открытые остаются открытыми.")
        return True

    # Parse "1, 3, 5" or "1 3 5" or "1\n3\n5"
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    if not numbers:
        await msg.reply_text("Не понял номера. Перечисли через запятую: «1, 3, 5» или /skip")
        return True

    open_ids = state.get("task_review_open_ids", [])
    closed = []
    failed = []
    try:
        ts = _import_todoist_sync(repo)
        for n in numbers:
            if n < 1 or n > len(open_ids):
                failed.append(f"#{n} (out of range)")
                continue
            tid = open_ids[n - 1]
            try:
                ok = ts.close_task(tid)
                if ok:
                    closed.append(n)
                else:
                    failed.append(f"#{n} (close_task returned False)")
            except Exception as e:
                failed.append(f"#{n} ({type(e).__name__})")
    except Exception:
        logger.exception("task_review: todoist close failed")
        await msg.reply_text("⚠️ Ошибка при закрытии в Todoist. Попробуй вручную.")
        return True

    state["task_review_active"] = False
    _save_state(repo, state)

    parts = []
    if closed:
        parts.append(f"✅ Закрыл в Todoist: {', '.join(f'#{n}' for n in closed)}")
    if failed:
        parts.append(f"⚠️ Не получилось: {', '.join(failed)}")
    parts.append("\n_PM check-in через 30 минут._")
    await msg.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)
    return True
