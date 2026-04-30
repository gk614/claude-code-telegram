"""Slash commands operating on the last router-written record: /undo /move /fix.

These commands depend on the last-action storage in
``scripts/inbox_router.py`` (record_last_action / get_last_action /
undo_last_action / clear_last_action). Each writable router handler records
its write so the user can roll it back, re-categorise it, or have Sonnet
patch it via natural language.

- /undo                 — remove the last router-written line from its file
- /move                 — show keyboard with 5 other categories, route there instead
- /fix <инструкция>     — pass the last record + instruction to Sonnet for editing
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

import structlog
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

logger = structlog.get_logger()


_CATEGORY_ICON = {
    "idea": "💡",
    "task": "✅",
    "journal": "📓",
    "metric": "📊",
    "food": "🍳",
    "bookmark": "📚",
}
_ALL_WRITABLE = ["idea", "task", "journal", "metric", "food", "bookmark"]


def _import_router() -> Any:
    """Import inbox_router lazily so this module loads even if path injection
    hasn't run yet at import time."""
    import inbox_router  # type: ignore[import-not-found]

    return inbox_router


def _short(text: str, n: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# /undo
# ---------------------------------------------------------------------------


async def handle_undo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Undo the last router-written record for this user."""
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id

    try:
        router = _import_router()
    except ImportError as exc:
        logger.error("inbox_router not importable for /undo", error=str(exc))
        await update.message.reply_text(
            "Router недоступен (не импортирован). Проверь GENAOS_REPO_PATH."
        )
        return

    action = router.undo_last_action(user_id)
    if action is None:
        await update.message.reply_text(
            "Нечего откатывать (или файл редактирован вручную и запись не нашлась)."
        )
        return

    router.log_undo(action, user_id, datetime.now(UTC))

    icon = _CATEGORY_ICON.get(action["category"], "·")
    await update.message.reply_text(
        f"↩️ Откатил: {icon} {_short(action['paraphrase'], 80)}\n"
        f"из {action['target_file']}"
    )


# ---------------------------------------------------------------------------
# /move
# ---------------------------------------------------------------------------


def _build_move_keyboard(
    current_category: str, message_id: int
) -> InlineKeyboardMarkup:
    """Inline keyboard with 5 alternative categories + cancel."""
    others = [c for c in _ALL_WRITABLE if c != current_category]
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for c in others:
        row.append(
            InlineKeyboardButton(
                f"{_CATEGORY_ICON[c]} {c}",
                callback_data=f"router_move:{c}:{message_id}",
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(
        [
            InlineKeyboardButton(
                "🗑 отмена", callback_data=f"router_move:cancel:{message_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def handle_move(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show inline keyboard with alternative categories for the last record."""
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id

    try:
        router = _import_router()
    except ImportError as exc:
        logger.error("inbox_router not importable for /move", error=str(exc))
        await update.message.reply_text("Router недоступен.")
        return

    action = router.get_last_action(user_id)
    if action is None:
        await update.message.reply_text(
            "Нечего перемещать (нет последнего действия router'а)."
        )
        return

    keyboard = _build_move_keyboard(action["category"], action["message_id"])
    icon = _CATEGORY_ICON.get(action["category"], "·")
    await update.message.reply_text(
        f"Куда переместить?\n"
        f"Сейчас: {icon} {action['category']} — {_short(action['paraphrase'], 60)}",
        reply_markup=keyboard,
    )


async def handle_move_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``router_move:<category>:<message_id>`` keyboard taps."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "router_move":
        return

    new_category = parts[1]
    try:
        expected_msg_id = int(parts[2])
    except ValueError:
        return

    user_id = query.from_user.id if query.from_user else 0
    chat_id = query.message.chat.id if query.message else 0

    try:
        router = _import_router()
    except ImportError as exc:
        logger.error("inbox_router not importable for move callback", error=str(exc))
        return

    action = router.get_last_action(user_id)

    # Stale-click guard: another message has overwritten last-action since the
    # keyboard was sent. Don't act.
    if action is None or action["message_id"] != expected_msg_id:
        try:
            await query.edit_message_text(
                "Уже не актуально — была новая запись router'а."
            )
        except Exception:
            pass
        return

    if new_category == "cancel":
        try:
            icon = _CATEGORY_ICON.get(action["category"], "·")
            await query.edit_message_text(
                f"Отмена. Запись осталась: {icon} {action['category']}."
            )
        except Exception:
            pass
        return

    if new_category not in _ALL_WRITABLE:
        try:
            await query.edit_message_text(f"Неизвестная категория: {new_category}.")
        except Exception:
            pass
        return

    # Undo current write, then re-route the verbatim original via manual override.
    undone = router.undo_last_action(user_id)
    if undone is None:
        try:
            await query.edit_message_text(
                "Не получилось откатить (файл изменён вручную). Перемещение отменено."
            )
        except Exception:
            pass
        return
    router.log_undo(undone, user_id, datetime.now(UTC))

    try:
        result = router.apply_manual_override(
            category=new_category,
            raw_text=action["raw_text"],
            user_id=user_id,
            message_id=expected_msg_id,
            chat_id=chat_id,
            ts=datetime.now(UTC),
        )
    except Exception as exc:
        logger.exception("/move re-route failed", error=str(exc))
        try:
            await query.edit_message_text(
                f"Перемещение упало: {exc}. Используй /undo и попробуй снова."
            )
        except Exception:
            pass
        return

    reply = result.transparency_reply or f"Перемещено в {new_category}."
    try:
        await query.edit_message_text(f"➡️ Перемещено\n{reply}")
    except Exception:
        logger.exception("move callback edit_message_text failed")


# ---------------------------------------------------------------------------
# /fix
# ---------------------------------------------------------------------------


_FIX_HELP_TEMPLATE = (
    "Используй: <code>/fix &lt;что поправить&gt;</code>\n"
    "Например: <code>/fix добавь срок до пятницы</code>\n\n"
    "Текущая запись:\n"
    "{icon} {paraphrase}\n"
    "Файл: <code>{target_file}</code>"
)


def _build_fix_prompt(action: dict, instruction: str) -> str:
    """Strict prompt: one Read of the target file, one Edit, one short
    confirmation. No exploration, no other files read.

    The prompt fights Sonnet/Haiku's default "orient yourself first" instinct
    that otherwise burns turns on Read of CLAUDE.md / Strategy.md / etc."""
    return (
        "[Запрос на правку записи inbox-router'а]\n\n"
        "СТРОГО ОГРАНИЧЕНИЕ — выполни ровно три шага:\n"
        f"1. Read({action['target_file']}) — ТОЛЬКО этот файл\n"
        f"2. Edit({action['target_file']}, ...) — измени строку с парафразой "
        "и blockquote-оригиналом (если он есть)\n"
        "3. Ответь ОДНОЙ короткой строкой: что именно изменил\n\n"
        "ЗАПРЕЩЕНО:\n"
        "- читать любые другие файлы (CLAUDE.md, Strategy.md, README, прочее)\n"
        "- использовать Glob, Grep, Bash, ls, find\n"
        "- задавать уточняющие вопросы\n"
        "- объяснять что-либо помимо одной строки в шаге 3\n\n"
        f"Файл: {action['target_file']}\n"
        f"Категория: {action['category']}\n"
        f"Текущая парафраза в файле: {action['paraphrase']}\n"
        f"Дословный оригинал пользователя (для контекста): {action['raw_text']}\n\n"
        f"Что поправить: {instruction}"
    )


async def handle_fix(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Pass the last record + instruction to Sonnet for in-place editing."""
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id

    try:
        router = _import_router()
    except ImportError as exc:
        logger.error("inbox_router not importable for /fix", error=str(exc))
        await update.message.reply_text("Router недоступен.")
        return

    action = router.get_last_action(user_id)
    if action is None:
        await update.message.reply_text(
            "Нечего поправлять — последнее действие router'а не найдено."
        )
        return

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        icon = _CATEGORY_ICON.get(action["category"], "·")
        await update.message.reply_text(
            _FIX_HELP_TEMPLATE.format(
                icon=icon,
                paraphrase=_short(action["paraphrase"], 120),
                target_file=action["target_file"],
            ),
            parse_mode="HTML",
        )
        return

    instruction = parts[1].strip()
    prompt = _build_fix_prompt(action, instruction)

    claude_integration = context.bot_data.get("claude_integration")
    if claude_integration is None:
        await update.message.reply_text("Claude integration недоступен.")
        return

    settings = context.bot_data.get("settings")
    current_dir = context.user_data.get(
        "current_directory",
        getattr(settings, "approved_directory", None),
    )
    session_id = context.user_data.get("claude_session_id")

    progress: Optional[Any] = await update.message.reply_text("✏️ Haiku правит запись…")

    # Cost defenses for /fix:
    #   - force_new=True: avoid auto-resuming the user's chat session (history
    #     would be billed every turn — measured at $0.32+ during smoke test).
    #   - claude_model = Haiku 4.5 for the duration of this call: ~3.75× cheaper
    #     than Sonnet on input, fine for one Read+Edit. Restored afterwards.
    #   - prompt explicitly forbids Glob/Grep/extra Reads (see _build_fix_prompt).
    config = getattr(claude_integration, "config", None)
    saved_model: Any = None
    swapped_model = False
    if config is not None and hasattr(config, "claude_model"):
        saved_model = config.claude_model
        try:
            config.claude_model = "claude-haiku-4-5-20251001"
            swapped_model = True
        except Exception:
            logger.exception("/fix could not swap to Haiku — falling back to Sonnet")

    try:
        claude_response = await claude_integration.run_command(
            prompt=prompt,
            working_directory=current_dir,
            user_id=user_id,
            session_id=None,
            force_new=True,
        )
    except Exception as exc:
        logger.exception("/fix run_command failed", error=str(exc))
        try:
            await progress.edit_text(f"Fix failed: {exc}")
        except Exception:
            pass
        return
    finally:
        if swapped_model and config is not None:
            try:
                config.claude_model = saved_model
            except Exception:
                logger.exception("/fix could not restore claude_model — set explicitly")

    # NOTE: deliberately NOT writing claude_response.session_id back to
    # context.user_data["claude_session_id"] — that's the user's main
    # conversation session, and we don't want a /fix subroutine to reset it.

    # The target file is now in a state Sonnet rewrote — our recorded
    # section_lines / row_index may no longer match. Clear the last_action
    # so /undo correctly says "nothing to undo" instead of silently failing.
    try:
        router.clear_last_action(user_id)
    except Exception:
        logger.exception("/fix clear_last_action failed (non-fatal)")

    content = (claude_response.content or "").strip() or "Готово."
    cost_suffix = ""
    cost = getattr(claude_response, "cost", 0.0) or 0.0
    if cost > 0:
        model_label = "Haiku" if swapped_model else "Sonnet"
        cost_suffix = (
            f"\n\n· ${cost:.4f} ({model_label}, {claude_response.num_turns} turns)"
        )
    body = content + cost_suffix
    if len(body) > 3500:
        body = body[:3500] + "…"
    try:
        await progress.edit_text(body)
    except Exception:
        # Falls back to a fresh reply if edit fails (e.g. message too old)
        await update.message.reply_text(body)


# ---------------------------------------------------------------------------
# /cost — show today's spend + baseline
# ---------------------------------------------------------------------------


async def handle_cost(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Any = None,
    **_kwargs: Any,
) -> None:
    """Show today's API cost trace summary + 7-day baseline."""
    from datetime import date, timedelta
    import re as _re

    if not update.effective_message:
        return

    repo = getattr(settings, "genaos_repo_path", None)
    if not repo:
        await update.effective_message.reply_text("genaos_repo_path не настроен")
        return

    costs_dir = Path(str(repo)) / "state" / "costs"
    cost_re = _re.compile(r"\$([0-9]+\.[0-9]+)")

    def _day_total(d):
        f = costs_dir / f"{d.isoformat()}.md"
        if not f.exists():
            return 0.0, 0
        total, n = 0.0, 0
        for line in f.read_text(encoding="utf-8").splitlines():
            m = cost_re.search(line)
            if m:
                try:
                    total += float(m.group(1))
                    n += 1
                except ValueError:
                    pass
        return total, n

    today = date.today()
    today_total, today_n = _day_total(today)
    last7 = [_day_total(today - timedelta(days=i + 1))[0] for i in range(7)]
    nonzero = [t for t in last7 if t > 0]
    baseline = sum(nonzero) / len(nonzero) if nonzero else 0.0

    msg = (
        f"💸 *Cost trace*\n"
        f"Сегодня: ${today_total:.4f} ({today_n} вызовов)\n"
        f"Baseline (7d avg): ${baseline:.4f}\n"
        f"Ratio: {(today_total / baseline if baseline > 0 else 0):.2f}× (alert при >2×)"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

