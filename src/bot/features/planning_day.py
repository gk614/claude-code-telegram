"""planning-day — /plan card + /key interactive selection.

/plan — beautiful daily card: 3 главных дела + ⭐ ключевые задачи +
встречи + тренировка + питание + что сделано.

/key — show open Todoist tasks numbered, Гена taps 3 → write
⭐ Ключевые block in episodic (Todoist label TODO when update_task lands).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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



def _episodic(repo: Path, day: str) -> Path:
    return repo / "tracks" / "state" / "episodic" / f"{day}.md"


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


def _refresh_todoist_to_plan(repo: Path) -> None:
    """Run scripts/todoist_to_plan.py to ensure ## План на сегодня is fresh."""
    py = "/root/GenaOS/.venv-mcp/bin/python"
    if not Path(py).exists():
        py = sys.executable
    try:
        env = os.environ.copy()
        env["GENAOS_REPO_PATH"] = str(repo)
        subprocess.run(
            [py, str(repo / "scripts" / "todoist_to_plan.py")],
            env=env, capture_output=True, timeout=15, check=False,
        )
    except Exception:
        logger.exception("planning-day: todoist sync failed (non-fatal)")


def _parse_plan_section(content: str) -> tuple[str, list[dict]]:
    """Parse ## План на сегодня section. Returns (full_section, items).

    items: [{"line": "...", "text": "...", "todoist_id": "..."|None, "done": bool, "key": bool}]
    """
    m = re.search(r"## План на сегодня\s*\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not m:
        return ("", [])
    body = m.group(1)
    items = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("###") or stripped.startswith("---"):
            continue
        # checkbox lines
        m_box = re.match(r"^- \[(.)\]\s*(.*?)(?:\s*<!--\s*todoist:([^\s>]+)\s*-->)?$", stripped)
        if m_box:
            done = m_box.group(1) == "x"
            text = m_box.group(2).strip()
            tid = m_box.group(3)
            is_key = "⭐" in text
            text_clean = text.replace("⭐", "").strip()
            items.append({
                "line": line,
                "text": text_clean,
                "todoist_id": tid,
                "done": done,
                "key": is_key,
            })
    return (body, items)


def _build_card(repo: Path, today: str) -> str:
    ep_content = _read(_episodic(repo, today))

    # Plan section
    _, items = _parse_plan_section(ep_content)
    open_items = [i for i in items if not i["done"]]
    done_items = [i for i in items if i["done"]]
    key_items = [i for i in items if i["key"]]

    # 3 главных дела from AM
    m_top3 = re.search(r"3 главных дел[аы]:?\s*(.+?)(?:\n[-#]|\Z)", ep_content, re.DOTALL | re.IGNORECASE)
    top3_text = ""
    if m_top3:
        raw = m_top3.group(1).strip()
        top3_text = raw.split("\n")[0].strip()[:150]

    # Workout slot for today (read from program.md / weekly_plan)
    from .workout_tracker import build_today_plan
    from datetime import date
    workout_md = build_today_plan(repo, date.fromisoformat(today))
    # Extract first heading line for compact display
    workout_first = workout_md.splitlines()[0].lstrip("#* ").strip() if workout_md else "—"

    # Habits done today (from check_in_state)
    cis = _load_state(repo)
    routine = cis.get("am_routine_checks", {})
    routine_done = sum(1 for v in routine.values() if v)
    am_done = bool(cis.get("am_answered"))
    pm_done = bool(cis.get("pm_answered"))

    # Compose card
    lines = [
        f"📋 *План на {today}*",
        "",
    ]
    if top3_text:
        lines += [f"🎯 *3 главных дела:* {top3_text}", ""]
    if key_items:
        lines.append("⭐ *Ключевые задачи:*")
        for k in key_items:
            mark = "✅" if k["done"] else "▢"
            lines.append(f"  {mark} {k['text']}")
        lines.append("")
    if open_items:
        lines.append(f"📋 *Все задачи на сегодня ({len(open_items)} open):*")
        for i, item in enumerate(items, 1):
            if item["done"]:
                lines.append(f"  {i}. ~~{item['text']}~~ ✅")
            else:
                star = "⭐ " if item["key"] else ""
                lines.append(f"  {i}. {star}{item['text']}")
        lines.append("")
    if not items:
        lines += ["📋 _Задачи не найдены — Todoist пуст или не синкается._", ""]

    lines += [f"💪 *Тренировка:* {workout_first}", ""]
    lines += [
        "📊 *Закрыто сегодня:*",
        f"  ☀️ Утренняя рутина: {routine_done}/4",
        f"  🌅 AM: {'✅' if am_done else '⏳'}",
        f"  🌙 PM: {'✅' if pm_done else '⏳ в 22:00'}",
        f"  ✅ Задач закрыто: {len(done_items)}",
        "",
        "_/key — выбрать 3 ключевых · /plan refresh — обновить из Todoist_",
    ]

    return "\n".join(lines)


async def send_plan_card(bot: Any, chat_id: int, repo: Path, refresh: bool = False) -> None:
    if refresh:
        _refresh_todoist_to_plan(repo)
    today = datetime.now(UTC).date().isoformat()
    text = _build_card(repo, today)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("plan card sent", today=today)


async def send_key_selector(bot: Any, chat_id: int, repo: Path) -> None:
    """/key — show open tasks numbered with inline buttons for selecting 3 keys."""
    _refresh_todoist_to_plan(repo)
    today = datetime.now(UTC).date().isoformat()
    ep_content = _read(_episodic(repo, today))
    _, items = _parse_plan_section(ep_content)
    open_items = [(i, item) for i, item in enumerate(items, 1) if not item["done"]]

    if not open_items:
        await bot.send_message(
            chat_id=chat_id,
            text="📋 _Открытых задач нет._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    cis = _load_state(repo)
    # TTL 10 min — buffer expires if /key was abandoned
    started_at = cis.get("key_selection_started_at")
    selected = set(cis.get("key_selection_buffer", []))
    if started_at:
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            if (datetime.now(UTC) - started_dt).total_seconds() > 600:
                selected = set()
        except Exception:
            selected = set()
    cis["key_selection_started_at"] = datetime.now(UTC).isoformat()

    text = (
        f"⭐ *Выбери 3 ключевых задачи*\n\n"
        + "\n".join(f"{i}. {item['text']}" for i, item in open_items[:20])
        + "\n\n_Тапай номера — отметятся ⭐. После 3-х тапни Подтвердить._"
    )

    rows = []
    nums = [i for i, _ in open_items[:12]]
    # 3-per-row grid
    for j in range(0, len(nums), 3):
        row = []
        for n in nums[j:j+3]:
            mark = "⭐" if n in selected else str(n)
            row.append(InlineKeyboardButton(mark, callback_data=f"keypick:toggle:{n}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✅ Подтвердить", callback_data="keypick:confirm"),
        InlineKeyboardButton("✖ Отмена", callback_data="keypick:cancel"),
    ])
    kb = InlineKeyboardMarkup(rows)

    cis["key_selection_buffer"] = list(selected)
    _save_state(repo, cis)

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


def _apply_key_marks(repo: Path, today: str, selected_indices: list[int]) -> list[str]:
    """Add ⭐ marker to selected lines in ## План на сегодня + create ### ⭐ Ключевые блок.
    Returns list of marked task texts."""
    ep_path = _episodic(repo, today)
    content = _read(ep_path)
    if not content:
        return []
    body, items = _parse_plan_section(content)
    if not body:
        return []

    open_items = [(i, item) for i, item in enumerate(items, 1) if not item["done"]]
    selected_items = [item for i, item in open_items if i in selected_indices]
    if not selected_items:
        return []

    selected_texts = [item["text"] for item in selected_items]
    selected_lines = {item["line"] for item in selected_items}

    # Build new body — add ⭐ to selected lines
    new_lines = []
    for line in body.splitlines():
        if line in selected_lines and "⭐" not in line:
            # insert ⭐ after [ ] checkbox
            new_lines.append(re.sub(r"^(- \[ \]\s*)(.*)$", r"\1⭐ \2", line))
        else:
            new_lines.append(line)
    new_body = "\n".join(new_lines)

    # Prepend ⭐ Ключевые сегодня block
    key_block = "\n### ⭐ Ключевые сегодня\n" + "\n".join(f"- ⭐ {t}" for t in selected_texts) + "\n\n### Все задачи\n"
    if "### ⭐ Ключевые сегодня" in new_body:
        # Replace existing block
        new_body = re.sub(
            r"### ⭐ Ключевые сегодня\s*\n(.*?)(?=\n### |\Z)",
            key_block.lstrip("\n"),
            new_body, count=1, flags=re.DOTALL,
        )
    else:
        new_body = key_block + new_body

    new_content = re.sub(
        r"(## План на сегодня\s*\n)(.*?)(?=\n## |\Z)",
        lambda m: m.group(1) + new_body,
        content, count=1, flags=re.DOTALL,
    )
    ep_path.write_text(new_content, encoding="utf-8")
    return selected_texts


async def handle_key_callback(update: Any, context: Any, settings: Any = None, **_kwargs: Any) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 2)
    if len(parts) < 2 or parts[0] != "keypick":
        await query.answer()
        return
    action = parts[1]

    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    cis = _load_state(repo)
    selected = set(cis.get("key_selection_buffer", []))
    bot = query.bot
    chat_id = query.message.chat_id if query.message else None

    if action == "toggle":
        try:
            n = int(parts[2])
        except (ValueError, IndexError):
            await query.answer()
            return
        if n in selected:
            selected.discard(n)
            await query.answer(f"#{n} убрана")
        else:
            if len(selected) >= 3:
                await query.answer("Уже 3 выбрано")
                return
            selected.add(n)
            await query.answer(f"⭐ #{n}")
        cis["key_selection_buffer"] = list(selected)
        _save_state(repo, cis)
        # Rebuild keyboard
        today = datetime.now(UTC).date().isoformat()
        ep_content = _read(_episodic(repo, today))
        _, items = _parse_plan_section(ep_content)
        open_items = [(i, item) for i, item in enumerate(items, 1) if not item["done"]]
        nums = [i for i, _ in open_items[:12]]
        rows = []
        for j in range(0, len(nums), 3):
            row = []
            for nm in nums[j:j+3]:
                mark = "⭐" if nm in selected else str(nm)
                row.append(InlineKeyboardButton(mark, callback_data=f"keypick:toggle:{nm}"))
            rows.append(row)
        rows.append([
            InlineKeyboardButton("✅ Подтвердить", callback_data="keypick:confirm"),
            InlineKeyboardButton("✖ Отмена", callback_data="keypick:cancel"),
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            pass
        return

    if action == "cancel":
        cis["key_selection_buffer"] = []
        _save_state(repo, cis)
        await query.answer("Отмена")
        try:
            await query.edit_message_text("✖ Выбор ключевых отменён.")
        except Exception:
            pass
        return

    if action == "confirm":
        if len(selected) == 0:
            await query.answer("Выбери хотя бы одну")
            return
        today = datetime.now(UTC).date().isoformat()
        marked = _apply_key_marks(repo, today, sorted(selected))
        cis["key_selection_buffer"] = []
        cis["key_tasks_today"] = marked
        _save_state(repo, cis)
        await query.answer(f"✅ Зафиксировано {len(marked)}")
        try:
            ack = "⭐ *Ключевые на сегодня:*\n" + "\n".join(f"  • {t}" for t in marked)
            await query.edit_message_text(ack, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        return

    await query.answer()
