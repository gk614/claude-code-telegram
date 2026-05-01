"""Heartbeat — proactive ping каждые 3ч в будни.

Шлёт ForceReply вопрос «Что делал?» — ответ Гены попадает в timeline-файл
`tracks/state/billing/<date>.md`. В 22:30 агрегатор (time_billing.py) читает
timeline, классифицирует через Haiku и пишет дневной биллинг в episodic.

Контракт: skills/time-billing/SKILL.md (v0.1 без Calendar; v0.2 с Calendar — Phase 1).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telegram import ForceReply

from . import _state_io

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


def _local_now() -> datetime:
    return datetime.now(UTC).astimezone(_LOCAL_TZ)


def _timeline_path(repo: Path, date_iso: str) -> Path:
    return repo / "tracks" / "state" / "billing" / f"{date_iso}.md"


def _ensure_timeline(p: Path, date_iso: str) -> None:
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: billing_timeline\ndate: {date_iso}\n---\n\n"
        f"# Timeline {date_iso}\n\n## Timeline\n\n",
        encoding="utf-8",
    )


async def send_heartbeat(bot: Any, chat_id: int, repo: Path) -> None:
    """Send proactive heartbeat ping with ForceReply."""
    now = _local_now()
    hhmm = now.strftime("%H:%M")
    text = (
        f"❤️ Heartbeat — {hhmm}\n\n"
        "Что делал последние ~3 часа? Это было в плане?\n\n"
        "🎙 Текст или голос. Запишу в дневной биллинг."
    )
    state = _state_io.load_state(repo)
    state["heartbeat_active"] = True
    state["heartbeat_sent_at"] = datetime.now(UTC).isoformat()
    _state_io.save_state(repo, state)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Что делал..."),
    )
    state = _state_io.load_state(repo)
    state["heartbeat_message_id"] = msg.message_id
    _state_io.save_state(repo, state)
    logger.info("heartbeat sent", chat_id=chat_id, hhmm=hhmm, msg=msg.message_id)


async def handle_heartbeat_reply(
    update_or_event: Any, context: Any = None, settings: Any = None, **_kwargs: Any
) -> bool:
    """Capture reply to heartbeat ping → append to timeline."""
    settings = settings or (context.bot_data.get("settings") if context else None)
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")

    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False

    state = _state_io.load_state(repo)
    if not state.get("heartbeat_active"):
        return False

    expected_id = state.get("heartbeat_message_id")
    if expected_id and msg.reply_to_message and msg.reply_to_message.message_id != expected_id:
        return False

    text = msg.text.strip()
    now = _local_now()
    today = now.date().isoformat()
    hhmm = now.strftime("%H:%M")

    p = _timeline_path(repo, today)
    _ensure_timeline(p, today)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- **{hhmm}** → {text}\n")

    state["heartbeat_active"] = False
    state.pop("heartbeat_sent_at", None)
    state.pop("heartbeat_message_id", None)
    _state_io.save_state(repo, state)

    try:
        await msg.reply_text(f"✅ В биллинг ({hhmm}). Спасибо.")
    except Exception:
        logger.exception("heartbeat: ack reply failed")

    logger.info("heartbeat reply written", date=today, hhmm=hhmm, text_len=len(text))
    return True
