"""Friday outreach check-in — Cycle 1 Goal 2.3.

Cron: 0 18 * * FRI (CST) → бот шлёт inline keyboard со счётчиками,
Гена нажимает → значение пишется в state.cycle1_outreach_log[<week>]
+ обновляется goals.json (2.3.outreach_total) + предложение
плана на сб-вс если <10/нед.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import _state_io

logger = structlog.get_logger(__name__)

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _LOCAL_TZ = UTC


def _local_today() -> datetime:
    return datetime.now(UTC).astimezone(_LOCAL_TZ)


def _iso_week_id(d: datetime) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _goals_path(repo: Path) -> Path:
    return repo / "state" / "cycle1" / "goals.json"


async def send_friday_outreach(bot: Any, chat_id: int, repo: Path) -> None:
    """Friday 18:00 CST — счётчик outreach за прошедшую неделю."""
    text = (
        "🎯 *Пятничный замер: outreach 2.3*\n\n"
        "Сколько outreach сделано с Сашей на этой неделе?\n"
        "_(холодные касания: письма / DM / звонки)_\n\n"
        "Цель: **10/нед × 12 нед = 120 за цикл**"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0", callback_data="fri_outreach:0"),
            InlineKeyboardButton("1-3", callback_data="fri_outreach:2"),
            InlineKeyboardButton("4-6", callback_data="fri_outreach:5"),
        ],
        [
            InlineKeyboardButton("7-9", callback_data="fri_outreach:8"),
            InlineKeyboardButton("10+ ✅", callback_data="fri_outreach:10"),
            InlineKeyboardButton("15+", callback_data="fri_outreach:15"),
        ],
        [InlineKeyboardButton("✏️ точное число", callback_data="fri_outreach:custom")],
    ])
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb,
    )


async def handle_friday_outreach_callback(
    update: Any, context: Any = None, settings: Any = None, **_kwargs: Any
) -> None:
    """Callback handler for fri_outreach:N."""
    settings = settings or (context.bot_data.get("settings") if context else None)
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")

    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 1)
    if parts[0] != "fri_outreach" or len(parts) != 2:
        await query.answer()
        return
    value = parts[1]

    if value == "custom":
        await query.answer("Введи число reply'ем")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        from telegram import ForceReply
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ Введи точное число outreach за неделю:",
            reply_markup=ForceReply(selective=False, input_field_placeholder="например 12"),
        )
        # Mark state so middleware catches reply
        state = _state_io.load_state(repo)
        state["fri_outreach_active"] = True
        state["fri_outreach_sent_at"] = datetime.now(UTC).isoformat()
        _state_io.save_state(repo, state)
        return

    try:
        n = int(value)
    except ValueError:
        await query.answer()
        return

    await query.answer(f"📝 {n}")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _record_outreach(query.message.chat_id, repo, n, context.bot)


async def handle_friday_outreach_reply(
    update_or_event: Any, context: Any = None, settings: Any = None, **_kwargs: Any
) -> bool:
    """ForceReply for /fri_outreach custom number."""
    settings = settings or (context.bot_data.get("settings") if context else None)
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False

    state = _state_io.load_state(repo)
    if not state.get("fri_outreach_active"):
        return False

    text = msg.text.strip()
    digits = "".join(c for c in text if c.isdigit() or c == ".")
    try:
        n = int(float(digits))
    except (ValueError, TypeError):
        await msg.reply_text("❓ Не нашёл число. Пример: 12. Попробуй ещё раз.")
        return True

    state["fri_outreach_active"] = False
    state.pop("fri_outreach_sent_at", None)
    _state_io.save_state(repo, state)

    bot = msg.get_bot()
    await _record_outreach(msg.chat_id, repo, n, bot)
    return True


async def _record_outreach(chat_id: int, repo: Path, n: int, bot: Any) -> None:
    """Write outreach count to state + cycle1/goals.json + send ack."""
    today = _local_today()
    week_id = _iso_week_id(today)

    state = _state_io.load_state(repo)
    log = state.setdefault("cycle1_outreach_log", {})
    log[week_id] = n
    _state_io.save_state(repo, state)

    # Update goals.json 2.3 outreach_total
    gp = _goals_path(repo)
    if gp.exists():
        try:
            goals = json.loads(gp.read_text())
            if "2.3" in goals.get("goals", {}):
                # Sum all weeks
                total = sum(log.values())
                goals["goals"]["2.3"]["outreach_total"] = total
                goals["goals"]["2.3"]["last_friday_count"] = n
                goals["goals"]["2.3"]["last_friday_week"] = week_id
                gp.write_text(json.dumps(goals, ensure_ascii=False, indent=2))
        except Exception:
            logger.exception("friday_outreach: goals.json update failed")

    # Compose response with motivation
    if n >= 10:
        emoji = "✅"
        verdict = "Цель недели достигнута. 12-нед норма — 120, держим темп."
    elif n >= 7:
        emoji = "🟡"
        verdict = f"До 10 нужно ещё {10-n}. План на выходные?"
    else:
        emoji = "🔴"
        verdict = f"<7 outreach. До 10 нужно ещё {10-n}. Сегодня + сб-вс — реально догнать?"

    total = sum(log.values())
    weeks_done = len(log)
    target_total = weeks_done * 10
    pct = (total / target_total * 100) if target_total > 0 else 0

    text = (
        f"{emoji} *Outreach {week_id}: {n}*\n\n"
        f"{verdict}\n\n"
        f"📊 Цикл-итог: {total} / {target_total} ({pct:.0f}%) за {weeks_done} нед"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("friday_outreach: send ack failed")
    logger.info("friday_outreach recorded", week=week_id, count=n, cycle_total=total)
