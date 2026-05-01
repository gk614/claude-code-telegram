"""Time-billing v0.1 — дневной агрегатор биллинга в 22:30.

Читает timeline-файл `tracks/state/billing/<date>.md`, через Haiku классифицирует
каждую запись в одну из 7 категорий, считает coverage + минуты по категориям,
пишет результат в episodic под `## Биллинг дня` и шлёт сводку Гене.

Также signal в reward-gate: добавляет conditions «coverage ≥80%» и «work_futura ≥4ч».

v0.1 — без Google Calendar, только timeline из heartbeat-ответов.
v0.2 — Calendar = source of truth, smart-pull при gap >90 мин (Phase 1).
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import structlog
import yaml

from . import _state_io

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


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


def _parse_timeline(text: str) -> List[Tuple[str, str]]:
    """Parse `- **HH:MM** → activity` lines. Returns [(hhmm, text), ...]."""
    entries: List[Tuple[str, str]] = []
    for line in text.splitlines():
        m = re.match(r"^\s*-\s*\*\*(\d{1,2}:\d{2})\*\*\s*→\s*(.+?)\s*$", line)
        if m:
            entries.append((m.group(1), m.group(2)))
    return entries


def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


async def _classify_via_haiku(entries: List[Tuple[str, str]], category_keys: List[str]) -> List[str]:
    """Batch-classify all timeline entries. Returns list of category keys."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("time_billing: no API key, defaulting all to unknown")
        return ["unknown"] * len(entries)
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        cats_str = ", ".join(category_keys)
        bullets = "\n".join(f"{i+1}. {hhmm} — {text}" for i, (hhmm, text) in enumerate(entries))
        prompt = (
            "Классифицируй каждую активность в одну из категорий.\n"
            "Категории и их семантика:\n"
            "- work_futura: работа над Futura (звонки, переговоры, документы, продажи, разбор почты)\n"
            "- family: время с Сашей/Ваней/Игорем, семья, прогулки/еда вместе\n"
            "- training: тренировка, бег, кор-стек, силовая, целевая прогулка\n"
            "- health_self: медитация, NSDR, холод, чтение (Theory U/Wilber/Замесин), journaling, дневной сон\n"
            "- network: интервью, коучи, новые знакомства, нетворкинг, GameDev tusovka\n"
            "- leisure: CS, Netflix, YouTube, скролл соцсетей, бесцельное в телефоне\n"
            "- unknown: непонятно или confidence низкое\n\n"
            f"Активности:\n{bullets}\n\n"
            f"Ответ строго в формате JSON массива из {len(entries)} строк-категорий "
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
            logger.warning("time_billing: Haiku no JSON, fallback unknown")
            return ["unknown"] * len(entries)
        cats = json.loads(m.group(0))
        normalized = []
        for c in cats:
            c = (c or "").strip().lower()
            normalized.append(c if c in category_keys else "unknown")
        # Pad/trim to length
        while len(normalized) < len(entries):
            normalized.append("unknown")
        return normalized[: len(entries)]
    except Exception:
        logger.exception("time_billing: Haiku classification failed")
        return ["unknown"] * len(entries)


def _aggregate_minutes(
    entries: List[Tuple[str, str]],
    cats: List[str],
    waking_start: dtime,
    waking_end: dtime,
) -> Tuple[Dict[str, int], int]:
    """For each entry, attribute time from previous entry (or waking_start) to itself
    in the matching category. Returns (mins_by_cat, total_window_min).
    """
    mins_by_cat: Dict[str, int] = {}
    total_window = (waking_end.hour * 60 + waking_end.minute) - (waking_start.hour * 60 + waking_start.minute)

    if not entries:
        return mins_by_cat, total_window

    # Sort entries chronologically
    sorted_pairs = sorted(zip(entries, cats), key=lambda e: _hhmm_to_minutes(e[0][0]))
    waking_start_min = waking_start.hour * 60 + waking_start.minute
    waking_end_min = waking_end.hour * 60 + waking_end.minute

    prev_min = waking_start_min
    for (hhmm, _text), cat in sorted_pairs:
        cur_min = _hhmm_to_minutes(hhmm)
        if cur_min <= waking_start_min:
            prev_min = cur_min
            continue
        if cur_min > waking_end_min:
            cur_min = waking_end_min
        delta = max(0, cur_min - prev_min)
        mins_by_cat[cat] = mins_by_cat.get(cat, 0) + delta
        prev_min = cur_min

    # Tail from last entry to waking_end → unknown
    tail = max(0, waking_end_min - prev_min)
    if tail > 0:
        mins_by_cat["unknown"] = mins_by_cat.get("unknown", 0) + tail

    return mins_by_cat, total_window


def _format_summary(mins_by_cat: Dict[str, int], total_window: int, cats_cfg: dict) -> str:
    """Build markdown summary block."""
    lines = ["## Биллинг дня", ""]
    known = sum(v for k, v in mins_by_cat.items() if k != "unknown")
    coverage = known / total_window if total_window > 0 else 0.0
    lines.append(f"**Coverage:** {coverage:.0%} ({known} / {total_window} мин)")
    lines.append("")
    lines.append("| | Категория | Минут | Цель | Статус |")
    lines.append("|---|---|---|---|---|")
    for key, cfg in cats_cfg.items():
        m = mins_by_cat.get(key, 0)
        if not isinstance(cfg, dict):
            continue
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
    """Append/replace ## Биллинг дня section in today's episodic."""
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
        # Replace existing section body
        txt = re.sub(
            r"## Биллинг дня.*?(?=\n## |\Z)",
            summary,
            txt, count=1, flags=re.DOTALL,
        )
    else:
        txt = txt.rstrip("\n") + "\n\n" + summary + "\n"
    ep.write_text(txt, encoding="utf-8")


async def aggregate_billing(bot: Any, chat_id: int, repo: Path) -> None:
    """22:30 cron handler — read timeline, classify, write episodic + signal."""
    cfg = _read_yaml(repo).get("time_billing", {})
    if not cfg.get("enabled", False):
        logger.info("time_billing aggregate skipped (disabled)")
        return

    today = _local_now().date().isoformat()
    timeline_path = repo / "tracks" / "state" / "billing" / f"{today}.md"
    if not timeline_path.exists():
        await bot.send_message(chat_id=chat_id, text="📊 Биллинг: timeline за сегодня пуст. (Heartbeat-ответов не было.)")
        return

    text = timeline_path.read_text(encoding="utf-8")
    entries = _parse_timeline(text)
    if not entries:
        await bot.send_message(chat_id=chat_id, text="📊 Биллинг: timeline есть, но записей не распарсилось.")
        return

    cats_cfg = cfg.get("categories", {})
    cat_keys = list(cats_cfg.keys()) or ["work_futura", "family", "training", "health_self", "network", "leisure", "unknown"]

    cats = await _classify_via_haiku(entries, cat_keys)

    waking_start = dtime.fromisoformat(cfg.get("waking_window_start", "09:00"))
    waking_end = dtime.fromisoformat(cfg.get("waking_window_end", "23:00"))

    mins_by_cat, total_window = _aggregate_minutes(entries, cats, waking_start, waking_end)
    summary = _format_summary(mins_by_cat, total_window, cats_cfg)

    _append_episodic(repo, today, summary)

    # Signal to reward-gate state
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
        logger.exception("time_billing: send summary failed")
        # Fallback without parse_mode
        try:
            plain = summary.replace("**", "").replace("*", "")
            await bot.send_message(chat_id=chat_id, text=plain)
        except Exception:
            pass

    logger.info("time_billing aggregate done", date=today, coverage=round(coverage, 2), mins=mins_by_cat)
