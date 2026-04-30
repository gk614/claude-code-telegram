"""presence-practices — daily slot reminders + feedback log.

Reads tracks/state/practices_library/library.md for descriptions.
Reads weekly_plans/<monday>.md (if exists) for 3-5 practices selected for the week.

Cron slots:
  07:00 (23:00 UTC) — morning practice (Wim Hof / Coherent / Cold)
  14:00 (06:00 UTC) — afternoon dip (NSDR / Cyclic Sighing / Walking)
  22:00 (14:00 UTC) — pre-sleep (4-7-8 / Body Scan / Coherent)

After each reminder — feedback prompt (1-10 rating).

Slash: /practice <name> — show how-to from library.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import structlog
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

logger = structlog.get_logger()


PRACTICE_LABELS = {
    "wim_hof": "🌅 Wim Hof Breathing",
    "cyclic_sighing": "☕→🧘 Cyclic Sighing",
    "nsdr": "💪→😌 NSDR 20 мин",
    "4_7_8": "🌙 4-7-8 Breathing",
    "yoga_nidra": "🏃→😌 Yoga Nidra Long",
    "box_breathing": "Box Breathing 4-4-4-4",
    "body_scan": "Body Scan",
    "metta": "Metta",
    "walking": "Walking Meditation",
    "cold": "Cold Exposure Breathing",
    "coherent_breathing": "Coherent 5-5",
}

QUICK_HOW_TO = {
    "wim_hof": "30-40 глубоких вдохов-выдохов → выдох до конца → задержка на пустых лёгких → восстанавливающий вдох + 15с задержки. 3 раунда, 12 мин. **Сидя/лёжа.**",
    "cyclic_sighing": "Глубокий вдох носом → короткий доvдох носом → длинный медленный выдох ртом. 5 минут.",
    "nsdr": "Ляг → закрой глаза → Huberman NSDR 10 или 20 мин на YouTube/Spotify.",
    "4_7_8": "Вдох носом 4с → задержка 7с → выдох ртом со звуком «шшш» 8с. 4 цикла.",
    "yoga_nidra": "Ляг → guided audio (Ally Boothroyd 35 мин) → следуй инструкциям.",
    "box_breathing": "Вдох 4с → задержка 4с → выдох 4с → задержка 4с. 4-8 циклов.",
    "body_scan": "Ляг → внимание от стоп до макушки, 30-60с на зону. Замечай, не оценивай.",
    "metta": "«Пусть я буду счастлив, здоров, в безопасности, в покое» → потом про близкого → нейтрального → трудного → всех.",
    "walking": "Иди медленнее обычного → внимание на стопы (контакт, перекат). Без телефона.",
    "cold": "Контрастный душ → последние 1-3 мин холод. Медленное дыхание носом, не задерживай.",
    "coherent_breathing": "Вдох 5с → выдох 5с, без задержек, носом. 10-20 мин.",
}


def _state_path(repo: Path) -> Path:
    return repo / "state" / "check_in_state.json"


def _load_state(repo: Path) -> dict:
    f = _state_path(repo)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(repo: Path, state: dict) -> None:
    f = _state_path(repo)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _week_practices(repo: Path) -> list[str]:
    """Read practices selected for current week from weekly_plans/<monday>.md."""
    today = datetime.now(UTC).date()
    from datetime import timedelta
    monday = today - timedelta(days=today.weekday())
    plan_file = repo / "tracks" / "state" / "weekly_plans" / f"{monday.isoformat()}.md"
    if not plan_file.exists():
        # Default — Wim Hof + Cyclic + NSDR + 4-7-8
        return ["wim_hof", "cyclic_sighing", "nsdr", "4_7_8"]
    text = _read(plan_file)
    m = re.search(r"## Практики недели\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    if not m:
        return ["wim_hof", "cyclic_sighing", "nsdr", "4_7_8"]
    body = m.group(1)
    selected = []
    for slug, label in PRACTICE_LABELS.items():
        # Match by emoji or partial text in label
        if any(part in body for part in label.split()[:3] if len(part) > 3):
            selected.append(slug)
    return selected or ["wim_hof", "cyclic_sighing", "nsdr", "4_7_8"]


def _pick_for_slot(week_practices: list[str], slot: str) -> Optional[str]:
    """Pick first practice from week_practices that fits the slot."""
    morning = ["wim_hof", "coherent_breathing", "cold", "metta"]
    afternoon = ["nsdr", "cyclic_sighing", "walking", "box_breathing"]
    evening = ["4_7_8", "body_scan", "coherent_breathing", "yoga_nidra"]
    candidates = {"morning": morning, "afternoon": afternoon, "evening": evening}.get(slot, [])
    for slug in candidates:
        if slug in week_practices:
            return slug
    return week_practices[0] if week_practices else None


async def _send_practice_reminder(bot: Any, chat_id: int, repo: Path, slot: str) -> None:
    week = _week_practices(repo)
    slug = _pick_for_slot(week, slot)
    if not slug:
        return
    label = PRACTICE_LABELS.get(slug, slug)
    how_to = QUICK_HOW_TO.get(slug, "_см. library.md_")

    slot_emoji = {"morning": "🌅", "afternoon": "☕→🧘", "evening": "🌙"}.get(slot, "🧘")

    text = (
        f"{slot_emoji} *{label}* — {slot}\n\n"
        f"_{how_to}_\n\n"
        f"После — оцени 1-10 как зашло."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(n), callback_data=f"prac_rate:{slug}:{n}") for n in (1, 2, 3, 4, 5)],
        [InlineKeyboardButton(str(n), callback_data=f"prac_rate:{slug}:{n}") for n in (6, 7, 8, 9, 10)],
        [InlineKeyboardButton("Skip", callback_data=f"prac_rate:{slug}:skip")],
    ])
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def send_morning_practice(bot: Any, chat_id: int, repo: Path) -> None:
    await _send_practice_reminder(bot, chat_id, repo, "morning")


async def send_afternoon_practice(bot: Any, chat_id: int, repo: Path) -> None:
    await _send_practice_reminder(bot, chat_id, repo, "afternoon")


async def send_evening_practice(bot: Any, chat_id: int, repo: Path) -> None:
    await _send_practice_reminder(bot, chat_id, repo, "evening")


async def handle_practice_callback(update: Any, context: Any, settings: Any = None, **_kwargs: Any) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 2)
    if parts[0] != "prac_rate" or len(parts) < 3:
        await query.answer()
        return
    slug, value = parts[1], parts[2]

    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    if value == "skip":
        await query.answer("Skip")
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    try:
        rating = int(value)
    except ValueError:
        await query.answer("?")
        return

    # Append to log.md
    log_file = repo / "tracks" / "state" / "practices_library" / "log.md"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if not log_file.exists():
        log_file.write_text("# Practices feedback log\n\n", encoding="utf-8")
    line = f"\n{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} | {slug} | rating {rating}/10"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line)

    label = PRACTICE_LABELS.get(slug, slug)
    emoji = "🟢" if rating >= 7 else ("🟡" if rating >= 5 else "🔴")
    await query.answer(f"{emoji} {rating}/10")
    try:
        await query.edit_message_text(f"✅ {label} → {rating}/10 записано в log.md")
    except Exception:
        pass
    logger.info("practice rated", slug=slug, rating=rating)


async def send_practice_show(bot: Any, chat_id: int, repo: Path, name: Optional[str] = None) -> None:
    """/practice slash — show today's planned practice OR specific by name."""
    if name:
        slug = name.lower().replace("-", "_").replace(" ", "_")
        if slug not in PRACTICE_LABELS:
            available = ", ".join(PRACTICE_LABELS.keys())
            await bot.send_message(
                chat_id=chat_id,
                text=f"❓ Не нашёл `{name}`. Доступные: {available}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        label = PRACTICE_LABELS[slug]
        how_to = QUICK_HOW_TO.get(slug, "_см. library.md_")
        text = f"*{label}*\n\n{how_to}"
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        return

    # No name — show today's planned ones
    week = _week_practices(repo)
    text = "🧘 *Practices на эту неделю:*\n\n" + "\n".join(
        f"  • {PRACTICE_LABELS.get(s, s)}" for s in week
    )
    text += "\n\n_/practice <name> — показать как делать._"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
