"""GenaOS-specific cron jobs. Registered once at bot startup (idempotent).

The default jobs read state/protocols/check_ins.yaml at fire time, so the
text of the questions / cron expressions / on/off flags can be tuned by
editing that YAML without restarting the bot.

Every job's prompt tells the agent to:
  1. Read state/protocols/check_ins.yaml
  2. Find its own slot
  3. Send the configured questions to Гена via Telegram
  4. (Where applicable) write the resulting Q&A back into the daily file

The agent is the one doing the actual interaction — JobScheduler just fires
the prompt and delivers the agent's reply to the configured chat_ids via
the event bus + NotificationService.
"""

from __future__ import annotations

from typing import Any, List, Optional

import structlog

logger = structlog.get_logger()

# Tag we put in job_name to recognise our jobs and avoid double-registration.
GENAOS_JOB_PREFIX = "genaos:"


_DEFAULT_JOBS: list[dict[str, Any]] = [
    {
        "slot": "am_check_in",
        "cron": "0 8 * * *",
        "prompt": (
            "Запусти AM check-in. "
            "Шаг 1: прочитай state/protocols/check_ins.yaml и достань список "
            "вопросов под `am_check_in.questions`. "
            "Шаг 2: задай ВСЕ вопросы одним сообщением Гене (не по одному — "
            "на сегодняшнем этапе MVP бот не ведёт многошаговую беседу из cron-job'ов; "
            "Гена ответит свободным текстом, его ответ позже разберёт agentic_text). "
            "Шаг 3: ничего не пиши в файл сейчас — это сделает обработчик ответа."
        ),
    },
    {
        "slot": "pm_check_in",
        "cron": "0 22 * * *",
        "prompt": (
            "Запусти PM check-in. "
            "Прочитай state/protocols/check_ins.yaml → `pm_check_in.questions`, "
            "задай вопросы одним сообщением Гене."
        ),
    },
    {
        "slot": "weekly_review_trigger",
        "cron": "0 18 * * 0",
        "prompt": (
            "Воскресенье 18:00 — время Weekly Review. "
            "Прочитай state/protocols/check_ins.yaml → `weekly_review.trigger_message` "
            "и пришли его Гене как короткое предложение начать review."
        ),
    },
    {
        "slot": "heartbeat",
        "cron": "0 11,14,17,20 * * 1-5",
        "prompt": (
            "Heartbeat-ping. Прочитай state/protocols/check_ins.yaml → `heartbeat`. "
            "ЕСЛИ `enabled: false` — НЕ отправляй ничего, return без действий. "
            "ЕСЛИ enabled — отправь `prompt_template` Гене."
        ),
    },
    {
        "slot": "non_negotiables_monitor",
        "cron": "0 13 * * *",  # 13:00 UTC = 21:00 CST
        "prompt": (
            "non_negotiables_monitor — обрабатывается напрямую через events/handlers.py "
            "(habit_check.send_non_negotiables_alert, deterministic Python, no LLM)."
        ),
    },
    {
        "slot": "never_miss_twice",
        "cron": "5 13 * * *",  # 13:05 UTC = 21:05 CST
        "prompt": (
            "never_miss_twice — обрабатывается напрямую через events/handlers.py "
            "(habit_check.send_never_miss_twice_alert, deterministic Python, no LLM)."
        ),
    },
    {
        "slot": "streaks_post_pm",
        "cron": "30 15 * * *",  # 15:30 UTC = 23:30 CST — после PM в 22:00
        "prompt": (
            "streaks_post_pm — обрабатывается напрямую через events/handlers.py "
            "(habit_check.update_streaks_after_pm, celebration only)."
        ),
    },
    {
        "slot": "task_review_pre_pm",
        "cron": "30 13 * * *",  # 13:30 UTC = 21:30 CST — за 30 мин до PM на 22:00
        "prompt": (
            "Task review pre-PM. Этот job НЕ обрабатывается через Sonnet — "
            "events/handlers.py перехватывает по job_name='genaos:task_review_pre_pm' "
            "и вызывает task_review.send_task_review() напрямую (Todoist API + ForceReply)."
        ),
    },
    {
        "slot": "workout_today",
        "cron": "30 23 * * *",  # 23:30 UTC = 07:30 CST — утренний план тренировки
        "prompt": (
            "workout-tracker — обрабатывается напрямую через events/handlers.py "
            "(workout_tracker.send_workout_today, deterministic Python, no LLM)."
        ),
    },
    {
        "slot": "whoop_age_weekly",
        "cron": "30 1 * * 0",
        "prompt": (
            "Воскресенье 09:30 локально (CST) — Whoop Healthspan check-in. "
            "Шаг 1: прочитай state/protocols/check_ins.yaml → `whoop_age_weekly.message` "
            "и пришли его Гене. "
            "Шаг 2: ничего больше не делай — Гена ответит позже свободным текстом "
            "(например «32.5 и 0.85»). Когда придёт его ответ, общий agentic-handler "
            "должен распознать что это reply на Whoop Healthspan ping (по recent context "
            "или по формату двух чисел) и допиши строку в tracks/body/whoop/healthspan.md "
            "в формате `- YYYY-MM-DD: Whoop Age <X>, Pace of Aging <Y>` где дата = today UTC+8. "
            "Подтверди коротко: «Записал — теперь N точек.» Если N >= 3, добавь: "
            "«Хочешь тренд + рекомендации? Отвечай /healthspan-review»."
        ),
    },
]


async def register_default_jobs(
    scheduler: Any,
    target_chat_ids: List[int],
    working_directory: Any,
    created_by: int = 0,
) -> None:
    """Idempotently add the GenaOS default cron jobs.

    Looks at scheduler.list_jobs() — anything already named `genaos:<slot>`
    is skipped, so restarts don't multiply the jobs.
    """
    try:
        existing = await scheduler.list_jobs()
    except Exception:
        logger.exception("Failed to list scheduler jobs; skipping registration")
        return

    existing_names: set[str] = {row.get("job_name", "") for row in existing}

    for spec in _DEFAULT_JOBS:
        job_name = f"{GENAOS_JOB_PREFIX}{spec['slot']}"
        if job_name in existing_names:
            logger.info("genaos job already registered, skipping", job_name=job_name)
            continue
        try:
            await scheduler.add_job(
                job_name=job_name,
                cron_expression=spec["cron"],
                prompt=spec["prompt"],
                target_chat_ids=target_chat_ids,
                working_directory=working_directory,
                created_by=created_by,
            )
            logger.info("genaos job registered", job_name=job_name, cron=spec["cron"])
        except Exception:
            logger.exception("Failed to register genaos job", job_name=job_name)
