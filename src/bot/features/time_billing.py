"""Time-billing v0.2 — Google Calendar = source of truth.

Workflow:
  - Heartbeat (smart-pull) каждые 15 мин в waking window:
    проверяет Calendar gap; если >90 мин без events → ForceReply ping
  - Reply создаёт Calendar event (start = last_event_end или waking_start, end = now)
  - В 22:30 cron агрегатор читает Calendar за день, через Haiku
    классифицирует events, считает coverage + минуты по 7 категориям
  - Пишет ## Биллинг дня в episodic + signal в reward-gate
  - /bill — snapshot now; /bill window 08:00 23:30 — edit waking window

Calendar ops via subprocess (.venv-mcp has google-api libs, bot venv doesn't).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import UTC, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
import yaml
from telegram import ForceReply

from . import _state_io

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


VENV_MCP = "/root/GenaOS/.venv-mcp/bin/python"
SCRIPT = "/root/GenaOS/scripts/billing_calendar.py"


def _local_now() -> datetime:
    return datetime.now(UTC).astimezone(_LOCAL_TZ)


def _read_yaml(repo: Path) -> dict:
    p = repo / "state" / "protocols" / "check_ins.yaml"
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _waking_window(repo: Path) -> Tuple[dtime, dtime]:
    """Read waking window from yaml or state.json override (set via /bill window)."""
    state = _state_io.load_state(repo)
    override = state.get("billing_window")  # {"start":"08:00","end":"23:30"}
    if isinstance(override, dict):
        try:
            s = dtime.fromisoformat(override["start"])
            e = dtime.fromisoformat(override["end"])
            return s, e
        except Exception:
            pass
    cfg = _read_yaml(repo).get("time_billing", {})
    s = dtime.fromisoformat(cfg.get("waking_window_start", "09:00"))
    e = dtime.fromisoformat(cfg.get("waking_window_end", "23:00"))
    return s, e


# ──────────────────────────────────────────────────────────────────────
# Calendar subprocess wrappers
# ──────────────────────────────────────────────────────────────────────

def _run_calendar_cmd(cmd: str, args_json: Optional[str] = None) -> Optional[Any]:
    """Run subprocess to .venv-mcp helper. Returns parsed JSON or None on error."""
    argv = [VENV_MCP, SCRIPT, cmd]
    if args_json is not None:
        argv.append(args_json)
    try:
        r = subprocess.run(argv, capture_output=True, timeout=20, text=True)
        if r.returncode != 0:
            logger.warning("calendar subprocess failed", cmd=cmd, stderr=r.stderr[:200])
            return None
        return json.loads(r.stdout.strip() or "null")
    except Exception:
        logger.exception("calendar subprocess error", cmd=cmd)
        return None


def _list_today_events(repo: Path) -> List[dict]:
    res = _run_calendar_cmd("list_today")
    return res if isinstance(res, list) else []


def _last_event_end(repo: Path) -> Optional[datetime]:
    res = _run_calendar_cmd("last_event_end")
    if not isinstance(res, dict):
        return None
    end = res.get("end")
    if not end:
        return None
    try:
        return datetime.fromisoformat(end).astimezone(_LOCAL_TZ)
    except Exception:
        return None


def _create_calendar_event(summary: str, start: datetime, end: datetime, description: str = "") -> Optional[dict]:
    """Create event via subprocess. Times are tz-aware datetimes."""
    args = {
        "summary": summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "description": description,
    }
    return _run_calendar_cmd("create_event", json.dumps(args))


# ──────────────────────────────────────────────────────────────────────
# Smart-pull (every 15 min cron in waking window)
# ──────────────────────────────────────────────────────────────────────

async def smart_pull_check(bot: Any, chat_id: int, repo: Path) -> None:
    """Run every 15 min. If gap >90 мин from last event → ping Гена."""
    cfg = _read_yaml(repo).get("time_billing", {})
    if not cfg.get("enabled", True):
        return

    now = _local_now()
    waking_start, waking_end = _waking_window(repo)
    now_t = now.time()
    if now_t < waking_start or now_t > waking_end:
        return  # outside waking

    state = _state_io.load_state(repo)
    # Avoid double-ping while a heartbeat is already pending
    if state.get("heartbeat_active"):
        return
    # Avoid pinging during PM check-in step-by-step
    if state.get("pm_active_question") not in (None, "", "done", "skipped"):
        return
    # Avoid spam — last smart-pull within 60 min
    last_pull = state.get("billing_smartpull_last")
    if last_pull:
        try:
            t = datetime.fromisoformat(last_pull)
            if (datetime.now(UTC) - t).total_seconds() < 60 * 60:
                return
        except Exception:
            pass

    last_end = _last_event_end(repo)
    waking_start_dt = datetime.combine(now.date(), waking_start, tzinfo=_LOCAL_TZ)
    anchor = last_end if last_end and last_end > waking_start_dt else waking_start_dt
    gap_min = int((now - anchor).total_seconds() / 60)
    if gap_min < 90:
        return

    # Trigger heartbeat — same UX
    from .heartbeat import send_heartbeat
    state["billing_smartpull_last"] = datetime.now(UTC).isoformat()
    state["billing_pull_anchor"] = anchor.isoformat()
    _state_io.save_state(repo, state)
    await send_heartbeat(bot, chat_id, repo)
    logger.info("smart_pull triggered heartbeat", gap_min=gap_min)


# ──────────────────────────────────────────────────────────────────────
# Heartbeat reply hook → create Calendar event
# ──────────────────────────────────────────────────────────────────────

async def write_heartbeat_to_calendar(repo: Path, activity_text: str) -> Optional[dict]:
    """After heartbeat reply: create Calendar event covering the gap."""
    now = _local_now()
    state = _state_io.load_state(repo)
    waking_start, _ = _waking_window(repo)
    waking_start_dt = datetime.combine(now.date(), waking_start, tzinfo=_LOCAL_TZ)

    anchor_str = state.get("billing_pull_anchor")
    anchor: Optional[datetime] = None
    if anchor_str:
        try:
            anchor = datetime.fromisoformat(anchor_str).astimezone(_LOCAL_TZ)
        except Exception:
            pass
    if anchor is None:
        last_end = _last_event_end(repo)
        anchor = last_end if last_end and last_end > waking_start_dt else waking_start_dt

    # Don't create a 0- or negative-duration event
    if anchor >= now:
        anchor = now - timedelta(minutes=15)

    summary = activity_text[:100]
    description = "via heartbeat ping (time-billing v0.2)"
    res = _create_calendar_event(summary, anchor, now, description)
    if res:
        state.pop("billing_pull_anchor", None)
        _state_io.save_state(repo, state)
    return res


# ──────────────────────────────────────────────────────────────────────
# Aggregate (22:30 cron)
# ──────────────────────────────────────────────────────────────────────

async def _classify_via_haiku(events: List[dict], category_keys: List[str]) -> List[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ["unknown"] * len(events)
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        cats_str = ", ".join(category_keys)
        bullets = "\n".join(
            f"{i+1}. {ev.get('summary','(no title)')} — {ev.get('description','')[:80]}"
            for i, ev in enumerate(events)
        )
        prompt = (
            "Классифицируй каждое Google Calendar event в одну из категорий:\n"
            "- work_futura: работа над Futura (звонки, переговоры, документы, продажи, разбор почты)\n"
            "- family: время с Сашей/Ваней/Игорем, семья, прогулки/еда вместе\n"
            "- training: тренировка, бег, кор-стек, силовая, целевая прогулка\n"
            "- health_self: медитация, NSDR, холод, чтение (Theory U/Wilber/Замесин), journaling, дневной сон\n"
            "- network: интервью, коучи, новые знакомства, нетворкинг, GameDev tusovka\n"
            "- leisure: CS, Netflix, YouTube, скролл соцсетей, бесцельное в телефоне\n"
            "- unknown: непонятно или confidence низкое\n\n"
            f"Events:\n{bullets}\n\n"
            f"Ответ строго в формате JSON массива из {len(events)} строк-категорий "
            f"(только из списка: {cats_str}). Никакого другого текста."
        )
        result = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (result.content[0].text or "").strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            return ["unknown"] * len(events)
        cats = json.loads(m.group(0))
        normalized = []
        for c in cats:
            c = (c or "").strip().lower()
            normalized.append(c if c in category_keys else "unknown")
        while len(normalized) < len(events):
            normalized.append("unknown")
        return normalized[: len(events)]
    except Exception:
        logger.exception("time_billing: Haiku classification failed")
        return ["unknown"] * len(events)


def _aggregate_minutes(
    events: List[dict],
    cats: List[str],
    waking_start: dtime,
    waking_end: dtime,
    today: datetime,
) -> Tuple[Dict[str, int], int]:
    """For each event, attribute its duration to its category, clipped to waking window.
    Gaps are added to 'unknown'.
    """
    waking_start_dt = datetime.combine(today.date(), waking_start, tzinfo=_LOCAL_TZ)
    waking_end_dt = datetime.combine(today.date(), waking_end, tzinfo=_LOCAL_TZ)
    total_window = int((waking_end_dt - waking_start_dt).total_seconds() / 60)

    mins_by_cat: Dict[str, int] = {}
    if not events:
        mins_by_cat["unknown"] = total_window
        return mins_by_cat, total_window

    intervals: List[Tuple[datetime, datetime, str]] = []
    for ev, cat in zip(events, cats):
        try:
            s = datetime.fromisoformat(ev["start"]).astimezone(_LOCAL_TZ)
            e = datetime.fromisoformat(ev["end"]).astimezone(_LOCAL_TZ)
        except Exception:
            continue
        s = max(s, waking_start_dt)
        e = min(e, waking_end_dt)
        if e <= s:
            continue
        intervals.append((s, e, cat))

    intervals.sort(key=lambda x: x[0])
    covered = 0
    cursor = waking_start_dt
    for s, e, cat in intervals:
        if s > cursor:
            gap = int((s - cursor).total_seconds() / 60)
            mins_by_cat["unknown"] = mins_by_cat.get("unknown", 0) + gap
        d = int((e - max(cursor, s)).total_seconds() / 60)
        if d > 0:
            mins_by_cat[cat] = mins_by_cat.get(cat, 0) + d
            covered += d
        cursor = max(cursor, e)
    if cursor < waking_end_dt:
        gap = int((waking_end_dt - cursor).total_seconds() / 60)
        mins_by_cat["unknown"] = mins_by_cat.get("unknown", 0) + gap
    return mins_by_cat, total_window


def _format_summary(mins_by_cat: Dict[str, int], total_window: int, cats_cfg: dict, header_extra: str = "") -> str:
    lines = ["## Биллинг дня"]
    if header_extra:
        lines.append(header_extra)
    lines.append("")
    known = sum(v for k, v in mins_by_cat.items() if k != "unknown")
    coverage = known / total_window if total_window > 0 else 0.0
    lines.append(f"**Coverage:** {coverage:.0%} ({known} / {total_window} мин)")
    lines.append("")
    lines.append("| | Категория | Минут | Цель | Статус |")
    lines.append("|---|---|---|---|---|")
    for key, cfg in cats_cfg.items():
        if not isinstance(cfg, dict):
            continue
        m = mins_by_cat.get(key, 0)
        emoji = cfg.get("emoji", "·")
        label = cfg.get("label", key)
        target = int(cfg.get("min_per_day") or 0)
        if target > 0:
            status = "✅" if m >= target else f"🟡 -{target-m}"
            lines.append(f"| {emoji} | {label} | {m} | {target} | {status} |")
        elif m > 0:
            lines.append(f"| {emoji} | {label} | {m} | — | — |")
    return "\n".join(lines)


def _append_episodic(repo: Path, today: str, summary: str) -> None:
    ep = repo / "tracks" / "state" / "episodic" / f"{today}.md"
    if not ep.exists():
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text(
            f"---\ndate: {today}\ntype: daily\nstatus: in_progress\n---\n\n"
            f"# {today}\n\n",
            encoding="utf-8",
        )
    txt = ep.read_text(encoding="utf-8")
    if "## Биллинг дня" in txt:
        txt = re.sub(
            r"## Биллинг дня.*?(?=\n## |\Z)",
            summary,
            txt, count=1, flags=re.DOTALL,
        )
    else:
        txt = txt.rstrip("\n") + "\n\n" + summary + "\n"
    ep.write_text(txt, encoding="utf-8")


async def aggregate_billing(bot: Any, chat_id: int, repo: Path) -> None:
    """22:30 cron — read Calendar today, classify, write episodic + signal."""
    cfg = _read_yaml(repo).get("time_billing", {})
    if not cfg.get("enabled", True):
        return

    today_dt = _local_now()
    today_iso = today_dt.date().isoformat()

    events = _list_today_events(repo)
    cats_cfg = cfg.get("categories", {})
    cat_keys = list(cats_cfg.keys()) or ["work_futura", "family", "training", "health_self", "network", "leisure", "unknown"]

    if events:
        cats = await _classify_via_haiku(events, cat_keys)
    else:
        cats = []

    waking_start, waking_end = _waking_window(repo)
    mins_by_cat, total_window = _aggregate_minutes(events, cats, waking_start, waking_end, today_dt)

    summary = _format_summary(mins_by_cat, total_window, cats_cfg)
    _append_episodic(repo, today_iso, summary)

    state = _state_io.load_state(repo)
    known = sum(v for k, v in mins_by_cat.items() if k != "unknown")
    coverage = known / total_window if total_window > 0 else 0.0
    state["billing_coverage"] = round(coverage, 3)
    state["billing_mins"] = mins_by_cat
    state["billing_aggregated_at"] = datetime.now(UTC).isoformat()
    _state_io.save_state(repo, state)

    try:
        await bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown")
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id, text=summary.replace("**", "").replace("*", ""))
        except Exception:
            logger.exception("time_billing: send summary failed")

    logger.info("billing aggregate done", date=today_iso, coverage=round(coverage, 2), mins=mins_by_cat)


# ──────────────────────────────────────────────────────────────────────
# /bill slash command
# ──────────────────────────────────────────────────────────────────────

async def handle_bill_command(bot: Any, chat_id: int, repo: Path, args: List[str]) -> None:
    """`/bill` — snapshot now. `/bill window HH:MM HH:MM` — set waking window."""
    if args and args[0] == "window":
        if len(args) < 3:
            await bot.send_message(chat_id=chat_id, text="Использование: `/bill window 08:00 23:30`", parse_mode="Markdown")
            return
        try:
            s = dtime.fromisoformat(args[1])
            e = dtime.fromisoformat(args[2])
            assert s < e
        except Exception:
            await bot.send_message(chat_id=chat_id, text="❌ Неправильный формат. Пример: `/bill window 08:00 23:30`", parse_mode="Markdown")
            return
        state = _state_io.load_state(repo)
        state["billing_window"] = {"start": args[1], "end": args[2]}
        _state_io.save_state(repo, state)
        await bot.send_message(chat_id=chat_id, text=f"✅ Окно бодрствования: {args[1]} → {args[2]}")
        return

    # snapshot now (mid-day version of aggregate)
    cfg = _read_yaml(repo).get("time_billing", {})
    cats_cfg = cfg.get("categories", {})
    cat_keys = list(cats_cfg.keys()) or ["work_futura", "family", "training", "health_self", "network", "leisure", "unknown"]

    events = _list_today_events(repo)
    if not events:
        await bot.send_message(chat_id=chat_id, text="📊 Calendar за сегодня пуст. Heartbeat-ответы создают events.")
        return

    cats = await _classify_via_haiku(events, cat_keys)
    today_dt = _local_now()
    waking_start, waking_end = _waking_window(repo)
    # Snapshot: use min(now, waking_end) as end-of-day for now
    snap_end = min(today_dt.time(), waking_end)
    mins_by_cat, total_window = _aggregate_minutes(events, cats, waking_start, snap_end, today_dt)

    summary = _format_summary(
        mins_by_cat, total_window, cats_cfg,
        header_extra=f"_(snapshot {today_dt.strftime('%H:%M')} — финал в 22:30)_",
    )
    try:
        await bot.send_message(chat_id=chat_id, text=summary, parse_mode="Markdown")
    except Exception:
        await bot.send_message(chat_id=chat_id, text=summary.replace("**", "").replace("*", ""))
