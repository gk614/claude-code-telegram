"""parking-lot — slash commands for managing inbox/parking_lot.md.

/parking — list last 10 ideas
/parking #N — show one
/parking kill #N <reason> — move to inbox/killed.md
/parking promote #N <slug> — create active_projects/PXX_<slug>.md, ⭐ remove from parking
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telegram.constants import ParseMode

logger = structlog.get_logger()


def _parking_path(repo: Path) -> Path:
    return repo / "inbox" / "parking_lot.md"


def _killed_path(repo: Path) -> Path:
    return repo / "inbox" / "killed.md"


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _parse_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse markdown table. Returns (header_cols, rows) where each row is list of cells."""
    lines = text.splitlines()
    rows = []
    in_table = False
    for line in lines:
        if "|" in line and "---" not in line:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if cells:
                rows.append(cells)
                in_table = True
        elif "---" in line and "|" in line:
            in_table = True
        elif in_table and not line.strip():
            break
    if not rows:
        return [], []
    header = rows[0]
    body = rows[1:]
    return header, body


def _list_ideas(repo: Path, limit: int = 10) -> list[dict]:
    text = _read(_parking_path(repo))
    _, rows = _parse_table(text)
    ideas = []
    for row in rows:
        if len(row) < 3:
            continue
        ideas.append({
            "n": row[0],
            "date": row[1] if len(row) > 1 else "",
            "idea": row[2] if len(row) > 2 else "",
            "source": row[3] if len(row) > 3 else "",
            "raw": row[4] if len(row) > 4 else "",
            "row": row,
        })
    return ideas[-limit:][::-1]  # last N, reversed (newest first)


def _find_by_id(repo: Path, n: str) -> dict | None:
    text = _read(_parking_path(repo))
    _, rows = _parse_table(text)
    for row in rows:
        if row and row[0].strip() == str(n).strip():
            return {"n": row[0], "date": row[1] if len(row) > 1 else "",
                    "idea": row[2] if len(row) > 2 else "",
                    "source": row[3] if len(row) > 3 else "",
                    "raw": row[4] if len(row) > 4 else "",
                    "row": row}
    return None


def _remove_id(repo: Path, n: str) -> str | None:
    """Remove row #n from parking_lot.md. Returns the removed line."""
    p = _parking_path(repo)
    text = _read(p)
    lines = text.splitlines()
    new_lines = []
    removed = None
    for line in lines:
        if "|" in line and not removed:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if cells and cells[0].strip() == str(n).strip():
                removed = line
                continue
        new_lines.append(line)
    if removed is not None:
        p.write_text("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return removed


async def send_parking_list(bot: Any, chat_id: int, repo: Path) -> None:
    from ._md_utils import escape_md
    ideas = _list_ideas(repo, limit=10)
    if not ideas:
        await bot.send_message(chat_id=chat_id, text="📦 _Parking lot пуст._", parse_mode=ParseMode.MARKDOWN)
        return
    parts = [f"📦 *Parking lot — последние {len(ideas)}:*\n"]
    for it in ideas:
        idea_short = it["idea"][:80] + ("…" if len(it["idea"]) > 80 else "")
        parts.append(f"  *#{it['n']}* ({escape_md(it['date'])}) {escape_md(idea_short)}")
    parts.append("\n_/parking #N — детали · /parking kill #N reason · /parking promote #N slug_")
    await bot.send_message(chat_id=chat_id, text="\n".join(parts), parse_mode=ParseMode.MARKDOWN)


async def send_parking_card(bot: Any, chat_id: int, repo: Path, n: str) -> None:
    from ._md_utils import escape_md
    item = _find_by_id(repo, n)
    if not item:
        await bot.send_message(chat_id=chat_id, text=f"❓ #{n} не найдена.", parse_mode=ParseMode.MARKDOWN)
        return
    text = (
        f"📦 *#{item['n']}* ({escape_md(item['date'])})\n\n"
        f"*Идея:* {escape_md(item['idea'])}\n"
        f"*Источник:* {escape_md(item['source'])}\n"
        f"*Оригинал:*\n> {escape_md(item['raw'])}\n\n"
        f"_/parking kill #{item['n']} reason · /parking promote #{item['n']} slug_"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def send_parking_kill(bot: Any, chat_id: int, repo: Path, n: str, reason: str) -> None:
    item = _find_by_id(repo, n)
    if not item:
        await bot.send_message(chat_id=chat_id, text=f"❓ #{n} не найдена.", parse_mode=ParseMode.MARKDOWN)
        return
    removed_line = _remove_id(repo, n)
    if not removed_line:
        await bot.send_message(chat_id=chat_id, text=f"❓ Не удалось удалить #{n}.", parse_mode=ParseMode.MARKDOWN)
        return
    # Append to killed.md
    killed = _killed_path(repo)
    today = datetime.now(UTC).date().isoformat()
    if not killed.exists():
        killed.write_text("# Killed ideas\n\n| # | Дата | Идея | Источник | Оригинал | reason | killed |\n|---|------|------|----------|----------|--------|--------|\n", encoding="utf-8")
    with killed.open("a", encoding="utf-8") as f:
        kill_line = removed_line.rstrip("|").rstrip() + f" reason: {reason} | {today} |"
        f.write(f"\n{kill_line}\n")
    await bot.send_message(
        chat_id=chat_id,
        text=f"🗑 #{n} → killed.md (reason: _{reason}_)\n_/undo чтобы вернуть_",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("parking kill", id=n, reason=reason)


async def send_parking_promote(bot: Any, chat_id: int, repo: Path, n: str, slug: str) -> None:
    item = _find_by_id(repo, n)
    if not item:
        await bot.send_message(chat_id=chat_id, text=f"❓ #{n} не найдена.", parse_mode=ParseMode.MARKDOWN)
        return

    # Find next PXX
    projects_dir = repo / "active_projects"
    projects_dir.mkdir(exist_ok=True)
    existing = [f.name for f in projects_dir.glob("P*.md")]
    max_n = 0
    for name in existing:
        m = re.match(r"P(\d+)_", name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    next_n = max_n + 1
    next_id = f"P{next_n:02d}"
    slug_clean = re.sub(r"[^\w-]+", "_", slug.lower())
    out = projects_dir / f"{next_id}_{slug_clean}.md"

    today = datetime.now(UTC).date().isoformat()
    out.write_text(
        f"---\nid: {next_id}\ncreated: {today}\nsource: parking_lot#{n}\nstatus: TBD\n---\n\n"
        f"# {next_id} — {slug_clean}\n\n"
        f"**Source:** parking_lot #{n} ({item['date']})\n\n"
        f"## Цель\nTODO:\n\n## Дедлайн\nTODO:\n\n## Бенефициар\nTODO:\n\n## Next action\nTODO:\n\n"
        f"## Original idea\n> {item['idea']}\n\n## Original raw\n> {item['raw']}\n",
        encoding="utf-8",
    )
    _remove_id(repo, n)

    await bot.send_message(
        chat_id=chat_id,
        text=f"🚀 #{n} → `active_projects/{next_id}_{slug_clean}.md`\n\n_Заполни Цель/Дедлайн/Next action когда будешь готов._",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("parking promote", id=n, project=next_id, slug=slug_clean)


async def handle_parking_command(bot: Any, chat_id: int, repo: Path, args: list[str]) -> None:
    """Dispatch /parking subcommands."""
    if not args:
        await send_parking_list(bot, chat_id, repo)
        return

    sub = args[0].lower()

    if sub.startswith("#"):
        n = sub[1:]
        await send_parking_card(bot, chat_id, repo, n)
        return

    if sub == "kill" and len(args) >= 2:
        target = args[1].lstrip("#")
        reason = " ".join(args[2:]) if len(args) >= 3 else "no reason"
        await send_parking_kill(bot, chat_id, repo, target, reason)
        return

    if sub == "promote" and len(args) >= 2:
        target = args[1].lstrip("#")
        slug = " ".join(args[2:]) if len(args) >= 3 else f"idea_{target}"
        await send_parking_promote(bot, chat_id, repo, target, slug)
        return

    if sub.isdigit():
        await send_parking_card(bot, chat_id, repo, sub)
        return

    await bot.send_message(
        chat_id=chat_id,
        text="📦 _/parking — list · /parking #N · /parking kill #N reason · /parking promote #N slug_",
        parse_mode=ParseMode.MARKDOWN,
    )
