"""planning-week — Sunday 19:00 local week planning flow.

Compact MVP:
  step 0 — context + auto-default schedule + edit option
  step 1 — слоты (review default Mon-Sun template)
  step 2 — 3-5 практик из library
  step 3 — 3 фокуса (ForceReply)
  step 4 — family commitments (ForceReply)
  done — write tracks/state/weekly_plans/<monday>.md

State: plan_week_active_step in check_in_state.json.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

logger = structlog.get_logger()


DEFAULT_SCHEDULE = [
    ("Пн", "💪 Силовая A (light)"),
    ("Вт", "🏃 Бег Качество"),
    ("Ср", "💪 Силовая B (medium)"),
    ("Чт", "🏃 Бег Лёгкий"),
    ("Пт", "💪 Силовая C (heavy upper)"),
    ("Сб", "😴 Отдых"),
    ("Вс", "🏃🏃 Длинный бег"),
]

DEFAULT_PRACTICES = [
    ("wim_hof", "🌅 Wim Hof (утро)"),
    ("cyclic_sighing", "☕→🧘 Cyclic Sighing (после обеда)"),
    ("nsdr", "💪→😌 NSDR 20 мин (после силовой)"),
    ("4_7_8", "🌙 4-7-8 (перед сном)"),
    ("yoga_nidra", "🏃→😌 Yoga Nidra Long (Вс)"),
    ("box_breathing", "Box Breathing (стресс)"),
    ("body_scan", "Body Scan (вечер)"),
    ("metta", "Metta (утро 2-3×)"),
    ("walking", "Walking Meditation"),
    ("cold", "Cold Exposure Breathing"),
]


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


def _next_monday(today: date) -> date:
    days_until_mon = (7 - today.weekday()) % 7
    if days_until_mon == 0 and today.weekday() == 0:
        days_until_mon = 0
    elif days_until_mon == 0:
        days_until_mon = 7
    return today + timedelta(days=days_until_mon)


def _cycle_week(repo: Path, target: date) -> int:
    program = (repo / "tracks" / "body" / "semantic" / "program.md").read_text(encoding="utf-8") if (repo / "tracks" / "body" / "semantic" / "program.md").exists() else ""
    m = re.search(r"cycle_start:\s*(\d{4}-\d{2}-\d{2})", program)
    if not m:
        return 1
    try:
        start = date.fromisoformat(m.group(1))
    except ValueError:
        return 1
    delta = (target - start).days
    return max(1, min(12, delta // 7 + 1))


# ──────────────────────────────────────────────────────────────────────
# Step 0 — context + start
# ──────────────────────────────────────────────────────────────────────

async def send_step0(bot: Any, chat_id: int, repo: Path) -> None:
    today = datetime.now(UTC).date()
    monday = _next_monday(today)
    if today.weekday() == 6:  # Sunday — plan for tomorrow's Monday
        monday = today + timedelta(days=1)
    week_num = _cycle_week(repo, monday)

    text = (
        f"🗓 *Планирование недели {monday.isoformat()} → +6 дней*\n"
        f"*Нед.{week_num} из 12 цикла*\n\n"
        f"4 шага: тренировки → практики → 3 фокуса → семья.\n\n"
        f"_~5-7 минут._\n\n"
        f"Готов?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Поехали", callback_data="pw_step:1")],
        [InlineKeyboardButton("Позже", callback_data="pw_step:later")],
    ])

    state = _load_state(repo)
    state["plan_week_active_step"] = "0"
    state["plan_week_monday"] = monday.isoformat()
    state["plan_week_cycle_week"] = week_num
    state.setdefault("plan_week_data", {})
    _save_state(repo, state)

    msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    state = _load_state(repo)
    state["plan_week_message_id"] = msg.message_id
    _save_state(repo, state)


# ──────────────────────────────────────────────────────────────────────
# Step 1 — training slots
# ──────────────────────────────────────────────────────────────────────

async def send_step1(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🗓 *1/4 — Тренировочные слоты*\n\n"
        "Default-расписание (Mon-Sun):\n"
        + "\n".join(f"  {day} — {label}" for day, label in DEFAULT_SCHEDULE)
        + "\n\n"
        "ОК принимаем default или хочешь править?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять default", callback_data="pw_slots:default")],
        [InlineKeyboardButton("✏️ Опишу свой расклад", callback_data="pw_slots:custom")],
    ])
    state = _load_state(repo)
    state["plan_week_active_step"] = "1"
    _save_state(repo, state)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ──────────────────────────────────────────────────────────────────────
# Step 2 — practices
# ──────────────────────────────────────────────────────────────────────

async def send_step2(bot: Any, chat_id: int, repo: Path) -> None:
    state = _load_state(repo)
    selected = set(state.get("plan_week_data", {}).get("practices", []))

    text = (
        "🗓 *2/4 — Practices на неделю*\n\n"
        "Выбери 3-5 (multi-select). Recommended на нед.1 цикла:\n"
        "Wim Hof + Cyclic Sighing + NSDR + 4-7-8 + Yoga Nidra Long.\n"
    )
    rows = []
    for slug, label in DEFAULT_PRACTICES:
        mark = "⭐ " if slug in selected else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"pw_prac:toggle:{slug}")])
    rows.append([InlineKeyboardButton("✅ Готово →", callback_data="pw_prac:done")])
    kb = InlineKeyboardMarkup(rows)

    state["plan_week_active_step"] = "2"
    _save_state(repo, state)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ──────────────────────────────────────────────────────────────────────
# Step 3 — 3 focus areas
# ──────────────────────────────────────────────────────────────────────

async def send_step3(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🗓 *3/4 — 3 фокуса недели*\n\n"
        "Стратегические направления, не задачи. Например:\n"
        "_«Финансы — закрыть P01» / «Бот — стабилизировать AM/PM» / «Семья — компенсация»_\n\n"
        "🎙 Голосом или текстом. Через запятую или новой строки."
    )
    state = _load_state(repo)
    state["plan_week_active_step"] = "3"
    _save_state(repo, state)
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="3 фокуса"),
    )


# ──────────────────────────────────────────────────────────────────────
# Step 4 — family commitments
# ──────────────────────────────────────────────────────────────────────

async def send_step4(bot: Any, chat_id: int, repo: Path) -> None:
    text = (
        "🗓 *4/4 — Family commitments*\n\n"
        "Заранее зафиксируй блоки на неделю:\n"
        "_«Завтраки с Ваней — Пн-Пт / ужин с Сашей пн ср пт / прогулка в сб / зоопарк вс»_\n\n"
        "🎙 Свободно."
    )
    state = _load_state(repo)
    state["plan_week_active_step"] = "4"
    _save_state(repo, state)
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=ForceReply(selective=False, input_field_placeholder="Family time"),
    )


# ──────────────────────────────────────────────────────────────────────
# Done — write weekly plan file
# ──────────────────────────────────────────────────────────────────────

async def send_done(bot: Any, chat_id: int, repo: Path) -> None:
    state = _load_state(repo)
    data = state.get("plan_week_data", {})
    monday = state.get("plan_week_monday")
    week = state.get("plan_week_cycle_week", 1)

    if not monday:
        return

    monday_d = date.fromisoformat(monday)
    end_d = monday_d + timedelta(days=6)

    slots_lines = []
    if data.get("slots_default", True):
        for day, label in DEFAULT_SCHEDULE:
            slots_lines.append(f"| {day} | {label} |")
    else:
        for line in (data.get("slots_custom", "")).splitlines():
            line = line.strip()
            if line:
                slots_lines.append(f"| — | {line} |")

    practices_lines = [f"- {p}" for p in data.get("practices_labels", [])]
    focus_text = data.get("focus", "_не указано_")
    family_text = data.get("family", "_не указано_")

    md = (
        f"---\n"
        f"type: episodic\n"
        f"track: state\n"
        f"week_start: {monday}\n"
        f"week_end: {end_d.isoformat()}\n"
        f"cycle_week: {week}\n"
        f"created: {datetime.now(UTC).isoformat()}\n"
        f"created_by: planning-week\n"
        f"---\n\n"
        f"# План недели {monday} → {end_d.isoformat()} (нед. {week} из 12)\n\n"
        f"## Тренировочные слоты\n\n"
        f"| День | Слот |\n|------|------|\n"
        + "\n".join(slots_lines)
        + "\n\n## Практики недели\n\n"
        + ("\n".join(practices_lines) if practices_lines else "_не выбраны_")
        + "\n\n## 3 фокуса\n\n"
        + focus_text
        + "\n\n## Family commitments\n\n"
        + family_text
        + "\n"
    )

    plans_dir = repo / "tracks" / "state" / "weekly_plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    out = plans_dir / f"{monday}.md"
    out.write_text(md, encoding="utf-8")

    state["plan_week_active_step"] = "done"
    state["plan_week_completed_at"] = datetime.now(UTC).isoformat()
    _save_state(repo, state)

    text = (
        f"✅ *План на неделю записан*\n"
        f"→ `tracks/state/weekly_plans/{monday}.md`\n\n"
        f"💪 Слоты: 3 силовых + 3 беговых + 1 отдых\n"
        f"🧘 Практики: {len(data.get('practices_labels', []))}\n"
        f"🎯 Фокусов: 3\n"
        f"👪 Family: задокументировано\n\n"
        f"_В понедельник 07:30 пришлю первую тренировку._"
    )
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    logger.info("plan_week done", monday=monday)


# ──────────────────────────────────────────────────────────────────────
# Callback handler
# ──────────────────────────────────────────────────────────────────────

async def handle_pw_callback(update: Any, context: Any, settings: Any = None, **_kwargs: Any) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":", 2)
    prefix = parts[0]

    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    data = state.setdefault("plan_week_data", {})
    bot = query.bot
    chat_id = query.message.chat_id if query.message else None

    if prefix == "pw_step":
        action = parts[1] if len(parts) > 1 else ""
        if action == "later":
            await query.answer("Позже")
            try:
                await query.edit_message_text("⏰ Отложено — попробуй `/plan_week` когда будешь готов.")
            except Exception:
                pass
            state["plan_week_active_step"] = "skipped"
            _save_state(repo, state)
            return
        if action == "1":
            await query.answer("Поехали")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_step1(bot, chat_id, repo)
            return

    if prefix == "pw_slots":
        action = parts[1] if len(parts) > 1 else ""
        if action == "default":
            data["slots_default"] = True
            state["plan_week_data"] = data
            _save_state(repo, state)
            await query.answer("Default принят")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_step2(bot, chat_id, repo)
            return
        if action == "custom":
            from telegram import ForceReply
            data["slots_default"] = False
            state["plan_week_data"] = data
            state["plan_week_active_step"] = "1_custom"
            _save_state(repo, state)
            await query.answer("Опишу")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await bot.send_message(
                chat_id=chat_id,
                text="✏️ Опиши свой расклад на неделю (по дням):",
                reply_markup=ForceReply(selective=False),
            )
            return

    if prefix == "pw_prac":
        action = parts[1] if len(parts) > 1 else ""
        selected = set(data.get("practices", []))
        if action == "toggle":
            slug = parts[2] if len(parts) > 2 else ""
            label_map = {s: l for s, l in DEFAULT_PRACTICES}
            label = label_map.get(slug, slug)
            if slug in selected:
                selected.discard(slug)
                await query.answer(f"убрана: {label}")
            else:
                if len(selected) >= 5:
                    await query.answer("Максимум 5")
                    return
                selected.add(slug)
                await query.answer(f"⭐ {label}")
            data["practices"] = list(selected)
            data["practices_labels"] = [label_map[s] for s in selected]
            state["plan_week_data"] = data
            _save_state(repo, state)
            # Rebuild keyboard
            rows = []
            for slug2, label2 in DEFAULT_PRACTICES:
                mark = "⭐ " if slug2 in selected else ""
                rows.append([InlineKeyboardButton(f"{mark}{label2}", callback_data=f"pw_prac:toggle:{slug2}")])
            rows.append([InlineKeyboardButton("✅ Готово →", callback_data="pw_prac:done")])
            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
            except Exception:
                pass
            return
        if action == "done":
            if not selected:
                await query.answer("Выбери хотя бы одну")
                return
            await query.answer(f"✅ {len(selected)} практик")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_step3(bot, chat_id, repo)
            return

    await query.answer()


async def handle_pw_text_reply(update_or_event: Any, context: Any = None, settings: Any = None, **_kwargs: Any) -> bool:
    """Process text reply during planning-week."""
    msg = update_or_event.effective_message if hasattr(update_or_event, "effective_message") else None
    if not msg or not msg.text:
        return False

    repo = Path(str(getattr(settings, "genaos_repo_path", "."))) if settings else Path(".")
    state = _load_state(repo)
    active = state.get("plan_week_active_step")
    if not active or active in ("done", "skipped", "0", "1", "2"):
        return False

    data = state.setdefault("plan_week_data", {})
    text = msg.text.strip()
    bot = msg.bot
    chat_id = msg.chat_id

    if active == "1_custom":
        data["slots_custom"] = text
        state["plan_week_data"] = data
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_step2(bot, chat_id, repo)
        return True

    if active == "3":
        data["focus"] = text
        state["plan_week_data"] = data
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_step4(bot, chat_id, repo)
        return True

    if active == "4":
        data["family"] = text
        state["plan_week_data"] = data
        _save_state(repo, state)
        await msg.reply_text("📝 Записано")
        await send_done(bot, chat_id, repo)
        return True

    return False
