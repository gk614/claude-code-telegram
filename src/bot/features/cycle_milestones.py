"""Cycle 1 milestone reviews — W4 / W8 / W12.

W4 (1 июня): первая контрольная точка — baseline установлен?
W8 (29 июня): прогресс ≥10% от W4 baseline?
W12 (26 июля): финал цикла — что закрыли, что в parking, что в Cycle 2?

Reads state/cycle1/goals.json, aggregates execution data, renders markdown
report and sends to Гена. Saves to tracks/state/weekly/2026-W{NN}_milestone.md.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


def _local_now() -> datetime:
    return datetime.now(UTC).astimezone(_LOCAL_TZ)


def _goals_path(repo: Path) -> Path:
    return repo / "state" / "cycle1" / "goals.json"


PHASE_INFO = {
    "w4": {
        "title": "W4 Milestone — 1 июня 2026",
        "context": "Конец первого месяца. Baseline должен быть установлен.",
        "questions": [
            "По каким целям baseline зафиксирован?",
            "Где провал — структурный (нереалистично) или временный (травма/перегруз)?",
            "Скорректировать W8/W12 milestones?",
        ],
    },
    "w8": {
        "title": "W8 Milestone — 29 июня 2026",
        "context": "Середина цикла. Должен быть прогресс ≥10% от W4 baseline.",
        "questions": [
            "Какие цели на трэке к W12?",
            "Какие срываются — что блокирует?",
            "Что переносим в Cycle 2?",
        ],
    },
    "w12": {
        "title": "W12 — Финал Cycle 1 — 26 июля 2026",
        "context": "Финал. Что закрыли / не закрыли / переносим. Personal narrative.",
        "questions": [
            "Final execution score по каждой цели",
            "Что в Cycle 1 удалось / не удалось / перенесено в Cycle 2",
            "12 нед назад я был... сейчас я...",
            "W13 (27 июля - 2 авг) = reflection week + Cycle 2 setup",
        ],
    },
}


def _execution_color(pct: float) -> str:
    if pct >= 0.85:
        return "🟢"
    if pct >= 0.70:
        return "🟡"
    return "🔴"


async def send_milestone_review(bot: Any, chat_id: int, repo: Path, phase: str) -> None:
    """Send milestone review card. phase = w4 | w8 | w12."""
    info = PHASE_INFO.get(phase)
    if not info:
        logger.warning("unknown milestone phase", phase=phase)
        return

    gp = _goals_path(repo)
    if not gp.exists():
        await bot.send_message(
            chat_id=chat_id,
            text=f"📅 *{info['title']}*\n\n_(state/cycle1/goals.json не найден)_",
            parse_mode="Markdown",
        )
        return

    try:
        goals_data = json.loads(gp.read_text())
    except Exception:
        logger.exception("milestone_review: goals.json parse failed")
        return

    goals = goals_data.get("goals", {})
    today = _local_now().date().isoformat()

    # Build summary by track
    tracks: dict[str, list[tuple[str, dict]]] = {}
    for goal_id, g in goals.items():
        track = g.get("track", "?")
        tracks.setdefault(track, []).append((goal_id, g))

    track_emoji = {
        "body": "💪",
        "business": "💼",
        "learning": "📚",
        "state": "🌱",
        "family": "👪",
        "money": "💰",
    }
    track_order = ["body", "business", "learning", "state", "family", "money"]

    lines = [
        f"📅 *{info['title']}*",
        "",
        f"_{info['context']}_",
        "",
        "## Execution per цель",
        "",
    ]

    on_track = 0
    behind = 0
    failing = 0

    for track in track_order:
        if track not in tracks:
            continue
        emoji = track_emoji.get(track, "·")
        lines.append(f"### {emoji} {track.upper()}")
        for goal_id, g in sorted(tracks[track]):
            title = g.get("title", goal_id)[:50]
            pct = g.get("execution_pct", 0)
            if isinstance(pct, (int, float)):
                color = _execution_color(pct / 100 if pct > 1 else pct)
                pct_display = f"{pct:.0f}%" if pct > 1 else f"{pct*100:.0f}%"
            else:
                color = "⚪"
                pct_display = "—"
            ms = g.get("milestones", {}).get(phase, "")
            ms_short = (ms[:60] + "…") if len(ms) > 60 else ms
            lines.append(f"  {color} **{goal_id}** {title} — {pct_display}")
            if ms_short:
                lines.append(f"    _W{phase[1:]} target:_ {ms_short}")

            if isinstance(pct, (int, float)):
                p = pct / 100 if pct > 1 else pct
                if p >= 0.85:
                    on_track += 1
                elif p >= 0.70:
                    behind += 1
                else:
                    failing += 1
        lines.append("")

    lines.append("## Summary")
    lines.append(f"  🟢 on track: {on_track}")
    lines.append(f"  🟡 behind: {behind}")
    lines.append(f"  🔴 failing: {failing}")
    lines.append("")
    lines.append("## Вопросы для рефлексии")
    for q in info["questions"]:
        lines.append(f"  • {q}")
    lines.append("")
    lines.append(f"_Используй `/weekly` чтобы пройти 5-фазный protocol с детальным разбором._")

    text = "\n".join(lines)

    # Save to state
    out_dir = repo / "tracks" / "state" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    iso = _local_now().isocalendar()
    out = out_dir / f"{iso[0]}-W{iso[1]:02d}_milestone_{phase}.md"
    out.write_text(text, encoding="utf-8")

    try:
        # Telegram has 4096 char limit per message
        if len(text) > 4000:
            part1 = text[:4000]
            part2 = text[4000:]
            await bot.send_message(chat_id=chat_id, text=part1, parse_mode="Markdown")
            await bot.send_message(chat_id=chat_id, text=part2, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("milestone_review: send failed, fallback plain")
        try:
            plain = text.replace("**", "").replace("*", "").replace("_", "")
            await bot.send_message(chat_id=chat_id, text=plain[:4000])
        except Exception:
            pass

    logger.info("milestone_review sent", phase=phase, on_track=on_track, behind=behind, failing=failing)


async def check_w3_emergency_trigger(repo: Path) -> tuple[bool, list[str]]:
    """Run after weekly review at W3 — if 3+ goals execution <70% → trigger.
    Returns (should_trigger, list_of_failing_goal_ids).
    """
    gp = _goals_path(repo)
    if not gp.exists():
        return (False, [])
    try:
        goals_data = json.loads(gp.read_text())
    except Exception:
        return (False, [])
    goals = goals_data.get("goals", {})
    failing = []
    for goal_id, g in goals.items():
        pct = g.get("execution_pct", 100)
        if isinstance(pct, (int, float)):
            p = pct / 100 if pct > 1 else pct
            if p < 0.70:
                failing.append(goal_id)
    return (len(failing) >= 3, failing)
