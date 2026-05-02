"""Cycle 1 conditional alerts — 5 защитных триггеров.

Каждый alert имеет dedup cooldown — не более 1 same-type alert / N часов.
В одном fire выбирается max 1 alert по priority order.

Priority (от высокого к низкому):
  A1 alcohol_violation       — non-negotiable нарушен
  A2 speed_index_high        — Состояние трек, замедление
  A3 outreach_behind         — Цель 2.3 риск
  A4 execution_below         — week threshold not met
  A5 evening_excitement_late — recovery 1.3 защита

Fire by cron `0 21 * * *` (одновременно с non_negotiables_monitor — но они
отдельные сообщения).
"""
from __future__ import annotations

import json
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


# Priority — lower number = higher priority
ALERT_PRIORITY = {
    "alcohol_violation": 1,
    "speed_index_high": 2,
    "outreach_behind": 3,
    "execution_below": 4,
    "evening_excitement_late": 5,
}

# Dedup cooldown per alert (hours)
ALERT_COOLDOWN_HOURS = {
    "alcohol_violation": 12,
    "speed_index_high": 24,
    "outreach_behind": 24,
    "execution_below": 168,  # weekly
    "evening_excitement_late": 4,
}


def _recently_sent(state: dict, alert_id: str, hours: int) -> bool:
    sent = state.get("cycle1_alerts_sent", {})
    last = sent.get(alert_id)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return False
    return (datetime.now(UTC) - last_dt) < timedelta(hours=hours)


def _mark_sent(state: dict, alert_id: str) -> None:
    state.setdefault("cycle1_alerts_sent", {})[alert_id] = datetime.now(UTC).isoformat()


# ──────────────────────────────────────────────────────────────────────
# A1 — alcohol_violation
# ──────────────────────────────────────────────────────────────────────

def _check_alcohol_today(repo: Path) -> bool:
    """Return True if any food entry today contains alcohol marker."""
    today = _local_today().isoformat()
    f = repo / "tracks" / "body" / "food" / f"{today}.md"
    if not f.exists():
        return False
    txt = f.read_text(encoding="utf-8")
    # Markers from food-tracker
    alcohol_pattern = re.compile(
        r"🚨\s*АЛКОГОЛЬ|alcohol_violation|пиво|вино|виски|водка|джин|ром|коньяк|шампанск|просекко|алкогол",
        re.IGNORECASE,
    )
    return bool(alcohol_pattern.search(txt))


def _alcohol_message() -> str:
    return (
        "🚨 *Алкоголь сегодня*\n\n"
        "Это hard non-negotiable + обещание сыну до конца 2026.\n\n"
        "_Записываю в журнал без самобичевания._ Что случилось?"
    )


# ──────────────────────────────────────────────────────────────────────
# A2 — speed_index_high (avg last 3 days >7)
# ──────────────────────────────────────────────────────────────────────

def _collect_speed_index_last_n(repo: Path, n: int = 3) -> List[int]:
    """Read last N daily PM check-ins, extract speed_index."""
    out: List[int] = []
    today = _local_today()
    for i in range(1, n + 2):  # check N-2 days back to today
        d = today - timedelta(days=i - 1)
        f = repo / "tracks" / "state" / "episodic" / f"{d.isoformat()}.md"
        if not f.exists():
            continue
        txt = f.read_text(encoding="utf-8")
        m = re.search(r"Замедление[\s-]*индекс[:\s]+(\d+)", txt)
        if m:
            try:
                out.append(int(m.group(1)))
            except ValueError:
                pass
        if len(out) >= n:
            break
    return out


def _speed_avg(values: List[int]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _speed_message(avg: float, days: int) -> str:
    return (
        f"⚠️ *Замедление-индекс высокий*\n\n"
        f"Среднее за {days} дней: *{avg:.1f}/10*\n\n"
        "Цикл 1, трек Состояние: «не торопиться» — ключевая практика.\n\n"
        "Что выкидываем сегодня из плана?"
    )


# ──────────────────────────────────────────────────────────────────────
# A3 — outreach_behind (Friday <7)
# ──────────────────────────────────────────────────────────────────────

def _outreach_this_week(repo: Path) -> int:
    """Sum of outreach for current ISO week from state."""
    state = _state_io.load_state(repo)
    log = state.get("cycle1_outreach_log", {})
    today = _local_today()
    iso = today.isocalendar()
    week_id = f"{iso[0]}-W{iso[1]:02d}"
    return int(log.get(week_id, 0))


def _outreach_message(count: int) -> str:
    needed = max(0, 10 - count)
    return (
        f"📞 *Outreach отстаёт*\n\n"
        f"На этой неделе: *{count}* из 10 (Цель 2.3)\n\n"
        f"До конца недели нужно ещё **{needed}**.\n"
        "План на сб-вс?"
    )


# ──────────────────────────────────────────────────────────────────────
# A4 — execution_below (weekly aggregate <85%)
# ──────────────────────────────────────────────────────────────────────

def _read_goals(repo: Path) -> dict:
    f = repo / "state" / "cycle1" / "goals.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _failing_goals(repo: Path, threshold: float = 0.70) -> List[Tuple[str, float]]:
    """Return list of (goal_id, execution_pct) where pct < threshold."""
    data = _read_goals(repo)
    out = []
    for gid, g in data.get("goals", {}).items():
        pct = g.get("execution_pct", 100)
        if not isinstance(pct, (int, float)):
            continue
        p = pct / 100 if pct > 1 else pct
        if p < threshold:
            out.append((gid, p))
    return out


def _execution_message(failing: List[Tuple[str, float]]) -> str:
    if len(failing) >= 3:
        # W3 emergency trigger zone
        head = (
            f"🔴 *3+ цели <70% — W3 emergency review zone*\n\n"
            "Cycle 1 на грани срыва. Активируем W3 emergency review?\n\n"
            "1. Какие 13-15 целей держим\n"
            "2. Какие в parking_lot\n"
            "3. Без самобичевания — признаём перегруз\n\n"
        )
    else:
        head = (
            f"🟡 *Цели проседают*\n\n"
            f"Execution <70% по {len(failing)} цел{'и' if len(failing) == 1 else 'ям'}:\n\n"
        )
    bullets = "\n".join(f"  • {gid} — {p:.0%}" for gid, p in failing[:5])
    tail = "\n\n_Разберём в воскресном /weekly Phase 3._"
    return head + bullets + tail


# ──────────────────────────────────────────────────────────────────────
# A5 — evening_excitement_late (current >23:00 + user active)
# ──────────────────────────────────────────────────────────────────────

def _is_late_and_excited(state: dict) -> bool:
    """Detect late-night activity. Heuristic: check if PM still not answered after 23:30."""
    now_local = datetime.now(UTC).astimezone(_LOCAL_TZ)
    if now_local.hour < 23:
        return False
    if now_local.hour == 23 and now_local.minute < 30:
        return False
    # PM should be done by 23:00 normally
    if state.get("pm_answered"):
        return False
    return True


def _late_message() -> str:
    return (
        "🌙 *Поздно*\n\n"
        "Цель 1.3 (Recovery 85%) требует ранний отбой.\n"
        "Excitable вечерами — главный блокер цикла.\n\n"
        "Закрываем ноут?"
    )


# ──────────────────────────────────────────────────────────────────────
# Main entry
# ──────────────────────────────────────────────────────────────────────

async def check_and_send_alerts(bot: Any, chat_id: int, repo: Path) -> None:
    """Cron entry. Evaluate all 5, send max 1 by priority + cooldown."""
    state = _state_io.load_state(repo)
    candidates: List[Tuple[str, str]] = []  # [(alert_id, message)]

    # A1 alcohol
    if not _recently_sent(state, "alcohol_violation", ALERT_COOLDOWN_HOURS["alcohol_violation"]):
        if _check_alcohol_today(repo):
            candidates.append(("alcohol_violation", _alcohol_message()))

    # A2 speed
    if not _recently_sent(state, "speed_index_high", ALERT_COOLDOWN_HOURS["speed_index_high"]):
        speeds = _collect_speed_index_last_n(repo, n=3)
        if len(speeds) >= 2 and _speed_avg(speeds) > 7:
            candidates.append(("speed_index_high", _speed_message(_speed_avg(speeds), len(speeds))))

    # A3 outreach (Friday only)
    if not _recently_sent(state, "outreach_behind", ALERT_COOLDOWN_HOURS["outreach_behind"]):
        if _local_today().weekday() == 4:  # Friday
            count = _outreach_this_week(repo)
            if count < 7:
                candidates.append(("outreach_behind", _outreach_message(count)))

    # A4 execution
    if not _recently_sent(state, "execution_below", ALERT_COOLDOWN_HOURS["execution_below"]):
        failing = _failing_goals(repo, threshold=0.70)
        if failing:
            candidates.append(("execution_below", _execution_message(failing)))

    # A5 late
    if not _recently_sent(state, "evening_excitement_late", ALERT_COOLDOWN_HOURS["evening_excitement_late"]):
        if _is_late_and_excited(state):
            candidates.append(("evening_excitement_late", _late_message()))

    if not candidates:
        return

    # Sort by priority and send only 1
    candidates.sort(key=lambda x: ALERT_PRIORITY.get(x[0], 99))
    chosen_id, chosen_msg = candidates[0]

    try:
        await bot.send_message(chat_id=chat_id, text=chosen_msg, parse_mode="Markdown")
        _mark_sent(state, chosen_id)
        _state_io.save_state(repo, state)
        logger.info("cycle1_alert sent", alert=chosen_id, total_candidates=len(candidates))
    except Exception:
        logger.exception("cycle1_alert send failed", alert=chosen_id)
