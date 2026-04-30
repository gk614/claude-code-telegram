"""body-measurements — Sunday 09:00 talia + 1st of month full measurements.

Sends prompt with ForceReply. Parsing of replies in middleware.
"""

from __future__ import annotations

import json
import re
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
    from . import _state_io
    return _state_io.load_state(repo)



def _save_state(repo: Path, state: dict) -> None:
    from . import _state_io
    _state_io.save_state(repo, state)


async def send_waist_prompt(bot: Any, chat_id: int, repo: Path) -> None:
    """Sunday 09:00 — measure waist."""
    text = (
        "📏 *Воскресенье — замерь талию*\n\n"
        "На уровне пупка, спокойный живот, утром натощак.\n\n"
        "Напиши число в см (например `86.5`) или `/skip`."
    )
    state = _load_state(repo)
    state["measurement_active"] = "waist"
    _save_state(repo, state)
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="например 86.5"),
    )


async def send_full_measurements_prompt(bot: Any, chat_id: int, repo: Path) -> None:
    """1st of month 09:00 — full body measurements."""
    text = (
        "📏 *1-го числа — полные замеры*\n\n"
        "Утром натощак. Напиши все 5 чисел через запятую или новую строку:\n\n"
        "*талия / грудь / бицепс / бедро / икра* (см)\n"
        "_+ опционально % жира если есть Withings._\n\n"
        "Пример: `87, 102, 36, 60, 38, 18.5`"
    )
    state = _load_state(repo)
    state["measurement_active"] = "full"
    _save_state(repo, state)
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="талия,грудь,бицепс,бедро,икра"),
    )


async def handle_measurement_reply(update_or_event: Any, context: Any = None, settings: Any = None, **_kwargs: Any) -> bool:
    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False
    settings = settings or (context.bot_data.get("settings") if context else None)
    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    active = state.get("measurement_active")
    if not active:
        return False

    text = msg.text.strip()
    if text.lower() == "/skip":
        state["measurement_active"] = None
        _save_state(repo, state)
        await msg.reply_text("Skip — пропустил.")
        return True

    today = datetime.now(UTC).date().isoformat()
    month = today[:7]
    measurements_file = repo / "tracks" / "body" / "measurements" / f"{month}.md"
    measurements_file.parent.mkdir(parents=True, exist_ok=True)

    if active == "waist":
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            await msg.reply_text("Не понял число. Просто `86.5` или /skip")
            return True
        waist_cm = float(m.group(1).replace(",", "."))

        if not measurements_file.exists():
            measurements_file.write_text(
                f"---\ntype: episodic\ntrack: body\nsubtype: measurements\nmonth: {month}\nunit: cm\n---\n\n"
                f"# Замеры — {month}\n\n## Еженедельно (талия)\n\n| Дата | Талия (см) |\n|------|------------|\n",
                encoding="utf-8",
            )
        with measurements_file.open("a", encoding="utf-8") as f:
            f.write(f"| {today} | {waist_cm} |\n")

        state["measurement_active"] = None
        _save_state(repo, state)
        await msg.reply_text(f"📏 Талия {waist_cm} см → tracks/body/measurements/{month}.md")
        return True

    if active == "full":
        numbers = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text)]
        if len(numbers) < 5:
            await msg.reply_text("Нужно минимум 5 чисел: талия,грудь,бицепс,бедро,икра")
            return True
        waist, chest, biceps, hip, calf = numbers[:5]
        body_fat = numbers[5] if len(numbers) >= 6 else None

        if not measurements_file.exists():
            measurements_file.write_text(
                f"---\ntype: episodic\ntrack: body\nsubtype: measurements\nmonth: {month}\nunit: cm\n---\n\n"
                f"# Замеры — {month}\n\n",
                encoding="utf-8",
            )
        block = (
            f"\n## Ежемесячно ({today})\n\n"
            f"| Замер | См |\n|-------|-----|\n"
            f"| Талия | {waist} |\n| Грудь | {chest} |\n| Бицепс | {biceps} |\n"
            f"| Бедро | {hip} |\n| Икра | {calf} |\n"
        )
        if body_fat is not None:
            block += f"| % жира | {body_fat}% |\n"
        with measurements_file.open("a", encoding="utf-8") as f:
            f.write(block)

        state["measurement_active"] = None
        _save_state(repo, state)
        ack = f"📏 Полные замеры за {month} записаны:\n  Талия {waist} · Грудь {chest} · Бицепс {biceps} · Бедро {hip} · Икра {calf}"
        if body_fat is not None:
            ack += f" · % жира {body_fat}"
        await msg.reply_text(ack)
        return True

    return False
