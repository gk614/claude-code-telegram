"""Never-miss-twice expanded for Cycle 1 specific goals.

Triggers when 2+ consecutive days miss for any of these:
- Goal 1.1 training_or_zaryadka
- Goal 1.2 protein_target (>=140g)
- Goal 4.1 morning_or_evening_breath (Wim Hof or evening breath)
- Goal 4.2 ritual (morning/evening tea ritual)

Called from habit_check.send_never_miss_twice_alert at 21:05 CST as supplement.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Tuple

import structlog

from . import _state_io

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


def _local_today() -> date:
    return datetime.now(UTC).astimezone(_LOCAL_TZ).date()


def _check_training(repo: Path, d: date) -> bool:
    """Return True if training was done that day (workout or run file exists)."""
    iso = d.isoformat()
    return (
        (repo / "tracks" / "body" / "workouts" / f"{iso}.md").exists()
        or (repo / "tracks" / "body" / "runs" / f"{iso}.md").exists()
        or (repo / "tracks" / "body" / "daily_movement" / f"{iso}.md").exists()
    )


def _check_protein(repo: Path, d: date, target: int = 140) -> bool:
    """Return True if protein for that day >= target. Reads food/<date>.md."""
    iso = d.isoformat()
    f = repo / "tracks" / "body" / "food" / f"{iso}.md"
    if not f.exists():
        return False
    txt = f.read_text(encoding="utf-8")
    # Look for "Белок: NN г" or "Б NN" patterns
    matches = re.findall(r"Белок\s*:\s*(\d+)", txt)
    if matches:
        try:
            total = max(int(m) for m in matches)
            return total >= target
        except ValueError:
            pass
    # Fallback: sum «Б NN» from per-meal lines
    inline = re.findall(r"Б\s+(\d+)\s*/", txt)
    if inline:
        try:
            return sum(int(m) for m in inline) >= target
        except ValueError:
            pass
    return False


def _check_breath(repo: Path, d: date) -> bool:
    """Wim Hof morning OR evening breath — check episodic AM/PM mentions."""
    iso = d.isoformat()
    f = repo / "tracks" / "state" / "episodic" / f"{iso}.md"
    if not f.exists():
        return False
    txt = f.read_text(encoding="utf-8").lower()
    return any(k in txt for k in ("wim hof", "wim-hof", "вим хоф", "4-7-8", "body scan", "дыхание"))


def _check_ritual(repo: Path, d: date) -> bool:
    """Morning/evening ritual — heuristic: episodic mentions tea/чай/ritual."""
    iso = d.isoformat()
    f = repo / "tracks" / "state" / "episodic" / f"{iso}.md"
    if not f.exists():
        return False
    txt = f.read_text(encoding="utf-8").lower()
    return any(k in txt for k in ("чай", "ritual", "ритуал", "медитац", "тишина"))


def detect_consecutive_misses(repo: Path) -> List[Tuple[str, str, int]]:
    """Return list of (goal_id, habit_label, miss_streak) where streak >= 2."""
    today = _local_today()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)

    out = []

    # Goal 1.1 training
    miss_today = not _check_training(repo, today)
    miss_yesterday = not _check_training(repo, yesterday)
    if miss_today and miss_yesterday:
        out.append(("1.1", "Тренировка / зарядка", 2))

    # Goal 1.2 protein
    if not _check_protein(repo, today) and not _check_protein(repo, yesterday):
        out.append(("1.2", "Белок ≥140г", 2))

    # Goal 4.1 breath
    if not _check_breath(repo, today) and not _check_breath(repo, yesterday):
        out.append(("4.1", "Wim Hof / вечернее дыхание", 2))

    # Goal 4.2 ritual
    if not _check_ritual(repo, today) and not _check_ritual(repo, yesterday):
        out.append(("4.2", "Утренний / вечерний ритуал", 2))

    return out


def format_never_miss_alert(misses: List[Tuple[str, str, int]]) -> str:
    """Build Telegram message."""
    if not misses:
        return ""
    head = "🔴 *Never-miss-twice — Cycle 1*\n\n"
    lines = []
    for gid, label, n in misses[:4]:
        lines.append(f"  • Цель **{gid}** {label} — {n} дн. подряд miss")
    tail = "\n\n_Без самобичевания. Что блокирует?_"
    return head + "\n".join(lines) + tail
