"""Check-in answer middleware v3 — save raw + parse structured sections.

Order:
  1. Identify check-in reply
  2. Append raw text to ## AM check-in / ## PM рефлексия (audit trail)
  3. Parse: state (1-10), sleep hours, mention of breakfast / alcohol
  4. Update ## Состояние (Утро / Вечер) and ## Habits check sections
  5. Update state file
  6. Schedule confirmation reply async, raise ApplicationHandlerStop early
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog
from telegram.ext import ApplicationHandlerStop

logger = structlog.get_logger()


def _load_state(repo: Path) -> dict:
    f = repo / "state" / "check_in_state.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(repo: Path, state: dict) -> None:
    f = repo / "state" / "check_in_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _episodic_path(repo: Path) -> Path:
    today = datetime.now(UTC).date().isoformat()
    return repo / "tracks" / "state" / "episodic" / f"{today}.md"


def _ensure_episodic(p: Path) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        today = datetime.now(UTC).date().isoformat()
        p.write_text(
            f"---\ndate: {today}\ntype: daily\nstatus: in_progress\n---\n\n"
            f"# {today}\n\n"
            "## Состояние\n- Утро (1–10):\n- Вечер (1–10):\n\n"
            "---\n\n"
            "## AM check-in\n\n"
            "---\n\n"
            "## PM рефлексия\n\n"
            "---\n\n"
            "## Habits check _(non-negotiables)_\n"
            "- [ ] Медитация ≥10 мин\n"
            "- [ ] Завтрак с Ваней\n"
            "- [ ] AM check-in\n"
            "- [ ] PM check-in\n"
            "- [ ] Алкоголь = 0\n",
            encoding="utf-8",
        )
    return p.read_text(encoding="utf-8")


# ----- Parsers -----

def parse_state_score(text: str) -> Optional[int]:
    """Extract 1-10 state value. Look for 'состояние X' or first standalone 1-10."""
    m = re.search(r"состояни[ея][^0-9\n]{0,30}(\d{1,2})", text, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10:
            return n
    # Fallback: first standalone number 1..10 in first 200 chars
    head = text[:200]
    m = re.search(r"\b(10|[1-9])\b", head)
    if m:
        return int(m.group(1))
    return None


def parse_sleep_hours(text: str) -> Optional[float]:
    m = re.search(r"со[нм][\s,:]*([\d.,]+)\s*ч?", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def detect_breakfast_with_vanya(text: str) -> Optional[bool]:
    """Return True if mentioned positively, False if explicitly negated, None if absent."""
    t = text.lower()
    if "вани" in t or "ваней" in t or "ваня" in t:
        # Heuristic: any negation directly?
        if re.search(r"(не|нет)\s+(будет|было|с)", t):
            return False
        return True
    return None  # not mentioned


def detect_alcohol(text: str) -> Optional[bool]:
    """Return True for alcohol_zero=true, False if violated, None if not mentioned.
    Default behaviour (handled outside): treat as True (assume zero)."""
    t = text.lower()
    if re.search(r"\b(пил|выпил|выпит|алкогол|вино|пиво|водка|виски)\b", t):
        return False
    return None


# ----- Section updaters -----

def update_state_section(content: str, kind: str, value: int) -> str:
    if kind == "am":
        return re.sub(
            r"(- Утро \(1[–-]10\):)( *\d*)",
            rf"\1 {value}",
            content,
            count=1,
        )
    return re.sub(
        r"(- Вечер \(1[–-]10\):)( *\d*)",
        rf"\1 {value}",
        content,
        count=1,
    )


def update_habits(content: str, flags: dict) -> str:
    label_for = {
        "meditation": "Медитация ≥10 мин",
        "breakfast": "Завтрак с Ваней",
        "am_done": "AM check-in",
        "pm_done": "PM check-in",
        "alcohol": "Алкоголь = 0",
    }
    for key, val in flags.items():
        if val is None:
            continue
        label = label_for.get(key)
        if not label:
            continue
        mark = "x" if val else " "
        content = re.sub(
            rf"- \[[ xX]\] ({re.escape(label)})",
            rf"- [{mark}] \1",
            content,
            count=1,
        )
    return content


def append_raw_reply(content: str, kind: str, body: str) -> str:
    section = "## AM check-in" if kind == "am" else "## PM рефлексия"
    timestamp = datetime.now(UTC).strftime("%H:%M UTC")
    block = f"\n_{timestamp}_\n\n{body}\n"
    if section not in content:
        # Append section at end
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n{section}\n{block}\n"
        return content
    # Insert block right after section header (before next ---)
    pattern = re.compile(
        rf"({re.escape(section)}\n)(.*?)(\n---|\Z)",
        re.DOTALL,
    )
    def _ins(m):
        return m.group(1) + (m.group(2) or "") + block + (m.group(3) or "")
    new = pattern.sub(_ins, content, count=1)
    return new


# ----- Identify -----

def _identify_check_in(reply_text: str) -> Optional[str]:
    if not reply_text:
        return None
    if "AM check-in" in reply_text or "Утренняя рутина" in reply_text:
        return "am"
    if "PM check-in" in reply_text or "PM рефлексия" in reply_text:
        return "pm"
    return None


# ----- Confirmation reply (async fire-and-forget) -----

async def _confirm_async(msg: Any, label: str, parsed_state: Optional[int]) -> None:
    extra = f" Состояние: {parsed_state}." if parsed_state else ""
    try:
        await msg.reply_text(
            f"✅ {label} check-in сохранён.{extra} /undo если ошибся."
        )
    except Exception:
        logger.exception("check_in_answer: confirmation reply failed")


# ----- Main middleware -----

async def check_in_answer_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    msg = event.effective_message
    if not msg:
        return await handler(event, data)

    text = (msg.text or msg.caption or "").strip()
    if not text or text.startswith("/"):
        return await handler(event, data)

    settings = data.get("settings")
    repo_str = getattr(settings, "genaos_repo_path", None) if settings else None

    # ─── EARLY CHECK: PM step-by-step active flow ───
    if repo_str:
        try:
            cis_state = _load_state(Path(str(repo_str)))

            # task_review (21:30 pre-PM) — highest priority, comes before PM
            if cis_state.get("task_review_active"):
                from ..features.task_review import handle_task_review_reply
                consumed = await handle_task_review_reply(event, None, settings=settings)
                if consumed:
                    raise ApplicationHandlerStop

            pm_active = cis_state.get("pm_active_question")
            if pm_active in ("3", "5", "1_custom", "4_edit"):
                from ..handlers.check_in_pm_callback import handle_pm_text_reply
                consumed = await handle_pm_text_reply(event, None, settings=settings)
                if consumed:
                    raise ApplicationHandlerStop

            am_active = cis_state.get("am_active_question")
            if am_active in ("1", "3_custom", "4", "5"):
                from ..handlers.check_in_am_callback import handle_am_text_reply
                consumed = await handle_am_text_reply(event, None, settings=settings)
                if consumed:
                    raise ApplicationHandlerStop

            pw_active = cis_state.get("plan_week_active_step")
            if pw_active in ("1_custom", "3", "4"):
                from ..features.planning_week import handle_pw_text_reply
                consumed = await handle_pw_text_reply(event, None, settings=settings)
                if consumed:
                    raise ApplicationHandlerStop
        except ApplicationHandlerStop:
            raise
        except Exception:
            logger.exception("check_in_answer: step-by-step early-check failed")

    reply = getattr(msg, "reply_to_message", None)
    if reply is None:
        return await handler(event, data)
    reply_from = getattr(reply, "from_user", None)
    if reply_from is None or not getattr(reply_from, "is_bot", False):
        return await handler(event, data)

    reply_text = reply.text or reply.caption or ""
    kind = _identify_check_in(reply_text)
    if kind is None:
        return await handler(event, data)

    settings = data.get("settings")
    if not settings:
        return await handler(event, data)
    repo_str = getattr(settings, "genaos_repo_path", None)
    if not repo_str:
        return await handler(event, data)
    repo = Path(str(repo_str))

    # 1. Append raw + update structured sections
    p = _episodic_path(repo)
    content = _ensure_episodic(p)
    # Snapshot BEFORE modifications so /undo can restore exactly
    pre_snapshot = content

    state_value = parse_state_score(text)
    sleep_hrs = parse_sleep_hours(text)
    breakfast = detect_breakfast_with_vanya(text)
    alcohol_violated = detect_alcohol(text)  # True = OK, False = violated, None = absent

    # Apply parsed values to ## Состояние
    if state_value is not None:
        content = update_state_section(content, kind, state_value)

    # Append raw reply under section
    content = append_raw_reply(content, kind, text)

    # Update Habits
    cis = _load_state(repo)
    routine = cis.get("am_routine_checks", {}) if kind == "am" else cis.get("am_routine_checks", {})
    flags: dict = {}
    if kind == "am":
        flags["am_done"] = True
        # Meditation: tap state OR text mention
        meditated_via_tap = bool(routine.get("meditation", False))
        meditated_via_text = bool(re.search(r"медитац", text, re.IGNORECASE))
        flags["meditation"] = meditated_via_tap or meditated_via_text
    else:
        flags["pm_done"] = True
    if breakfast is not None:
        flags["breakfast"] = breakfast
    # Alcohol: True (zero) if not violated. Default to True only if user mentioned it negatively.
    if alcohol_violated is False:
        flags["alcohol"] = False
    else:
        # Don't claim done unless user explicitly says so OR it's the day-end PM reflection
        if kind == "pm":
            # Default assume zero unless violated
            flags["alcohol"] = True

    content = update_habits(content, flags)
    p.write_text(content, encoding="utf-8")

    # Register for /undo: store snapshot so undo restores byte-exact
    try:
        import sys as _sys
        if "/root/GenaOS/scripts" not in _sys.path:
            _sys.path.insert(0, "/root/GenaOS/scripts")
        from inbox_router import record_last_action as _record
        _record(
            user_id=event.effective_user.id if event.effective_user else 0,
            category="check_in_answer",
            target_file=str(p.relative_to(repo)),
            kind="check_in_block",
            raw_text=text,
            paraphrase=f"{kind.upper()} answer",
            message_id=msg.message_id,
            ts=datetime.now(UTC),
            episodic_snapshot=pre_snapshot,
        )
    except Exception:
        logger.exception("check_in_answer: record_last_action failed (non-fatal)")

    # 2. Update check_in_state
    if kind == "am":
        cis["am_answered"] = True
        cis["am_locked"] = False
    else:
        cis["pm_answered"] = True
        cis["pm_locked"] = False
    if sleep_hrs is not None:
        cis["sleep_hours_today"] = sleep_hrs
    _save_state(repo, cis)

    label = "AM" if kind == "am" else "PM"
    logger.info(
        "check_in_answer SAVED early-halt + parsed",
        kind=kind, state_value=state_value, sleep=sleep_hrs,
        breakfast=breakfast, alcohol_violated=alcohol_violated,
        preview=text[:60],
    )

    asyncio.create_task(_confirm_async(msg, label, state_value))
    raise ApplicationHandlerStop()
