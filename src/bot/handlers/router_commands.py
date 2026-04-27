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
    return (
        "[Запрос на правку записи inbox-router'а]\n"
        f"Файл: {action['target_file']}\n"
        f"Категория: {action['category']}\n"
        f"Текущая парафраза: {action['paraphrase']}\n"
        f"Дословный оригинал пользователя: {action['raw_text']}\n\n"
        f"Что поправить: {instruction}\n\n"
        "Открой указанный файл, найди эту запись (ищи по парафразе или по "
        "строке оригинала), отредактируй согласно инструкции, подтверди "
        "одной строкой что именно изменил."
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

    progress: Optional[Any] = await update.message.reply_text("✏️ Sonnet правит запись…")

    # IMPORTANT: force_new=True keeps /fix cheap. Without it, Sonnet auto-resumes
    # the user's running Telegram session and the entire chat history gets
    # billed as input on every turn — measured at ~$0.30+ per fix during
    # smoke testing. With force_new, only the prompt + Read of the target
    # file go in → typically <$0.02.
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
        cost_suffix = f"\n\n· ${cost:.4f} (Sonnet, {claude_response.num_turns} turns)"
    body = content + cost_suffix
    if len(body) > 3500:
        body = body[:3500] + "…"
    try:
        await progress.edit_text(body)
    except Exception:
        # Falls back to a fresh reply if edit fails (e.g. message too old)
        await update.message.reply_text(body)
