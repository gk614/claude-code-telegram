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
        "cron": "0 9 * * *",  # 09:00 CST — утренний check-in,
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
        "cron": "0 22 * * *",  # 22:00 CST — вечерний check-in,
        "prompt": (
            "Запусти PM check-in. "
            "Прочитай state/protocols/check_ins.yaml → `pm_check_in.questions`, "
            "задай вопросы одним сообщением Гене."
        ),
    },
    {
        "slot": "weekly_review_trigger",
        "cron": "0 18 * * 0",  # 18:00 CST вс — Weekly Review,  # 10:00 UTC = 18:00 CST вс
        "prompt": (
            "weekly_review — обрабатывается напрямую через events/handlers.py "
            "(weekly_review.send_aggregate, deterministic Python aggregate of all "
            "tracks for past 7 days + interactive 5-question reflection)."
        ),
    },
    {
        "slot": "plan_week_trigger",
        "cron": "0 19 * * 0",  # 19:00 CST вс — Plan-week,  # 11:00 UTC = 19:00 CST вс — после weekly review
        "prompt": (
            "plan_week — обрабатывается напрямую через events/handlers.py "
            "(planning_week.send_step0, deterministic Python)."
        ),
    },
    {
        "slot": "heartbeat",
        "cron": "0 12,15,18 * * 1-5",  # 12/15/18 CST будни — каждые 3ч в рабочий день
        "prompt": (
            "heartbeat — обрабатывается напрямую через events/handlers.py "
            "(features/heartbeat.send_heartbeat — ForceReply ping)."
        ),
    },
    {
        "slot": "billing_aggregate",
        "cron": "30 22 * * *",  # 22:30 CST — после PM check-in 22:00, перед reward_gate 23:00
        "prompt": (
            "billing_aggregate — обрабатывается напрямую через events/handlers.py "
            "(features/time_billing.aggregate_billing — Calendar + Haiku categorize)."
        ),
    },
    {
        "slot": "billing_smartpull",
        "cron": "*/15 9-22 * * *",  # каждые 15 мин с 09:00 до 22:00 CST
        "prompt": (
            "billing_smartpull — обрабатывается напрямую через events/handlers.py "
            "(features/time_billing.smart_pull_check — gap >90 мин в Calendar → ping)."
        ),
    },
    # Cycle 1 additions (since 2026-05-04)
    {
        "slot": "friday_outreach",
        "cron": "0 18 * * 5",  # 18:00 CST пятница — счётчик outreach
        "prompt": (
            "friday_outreach — обрабатывается напрямую через events/handlers.py "
            "(features/friday_outreach.send_friday_outreach)."
        ),
    },
    {
        "slot": "w4_milestone_review",
        "cron": "0 19 1 6 *",  # 19:00 CST 1 июня — конец Week 4
        "prompt": (
            "w4_milestone_review — обрабатывается напрямую через events/handlers.py "
            "(features/cycle_milestones.send_milestone_review with phase=w4)."
        ),
    },
    {
        "slot": "w8_milestone_review",
        "cron": "0 19 29 6 *",  # 19:00 CST 29 июня — конец Week 8
        "prompt": (
            "w8_milestone_review — features/cycle_milestones.send_milestone_review phase=w8."
        ),
    },
    {
        "slot": "w12_cycle_close",
        "cron": "0 19 26 7 *",  # 19:00 CST 26 июля — конец Cycle 1
        "prompt": (
            "w12_cycle_close — features/cycle_milestones.send_milestone_review phase=w12."
        ),
    },
    {
        "slot": "non_negotiables_monitor",
        "cron": "0 21 * * *",  # 21:00 CST,  # 13:00 UTC = 21:00 CST
        "prompt": (
            "non_negotiables_monitor — обрабатывается напрямую через events/handlers.py "
            "(habit_check.send_non_negotiables_alert, deterministic Python, no LLM)."
        ),
    },
    {
        "slot": "never_miss_twice",
        "cron": "5 21 * * *",  # 21:05 CST,  # 13:05 UTC = 21:05 CST
        "prompt": (
            "never_miss_twice — обрабатывается напрямую через events/handlers.py "
            "(habit_check.send_never_miss_twice_alert, deterministic Python, no LLM)."
        ),
    },
    {
        "slot": "streaks_post_pm",
        "cron": "30 23 * * *",  # 23:30 CST — после PM,  # 15:30 UTC = 23:30 CST — после PM в 22:00
        "prompt": (
            "streaks_post_pm — обрабатывается напрямую через events/handlers.py "
            "(habit_check.update_streaks_after_pm, celebration only)."
        ),
    },
    {
        "slot": "task_review_pre_pm",
        "cron": "30 21 * * *",  # 21:30 CST — за 30 мин до PM,  # 13:30 UTC = 21:30 CST — за 30 мин до PM на 22:00
        "prompt": (
            "Task review pre-PM. Этот job НЕ обрабатывается через Sonnet — "
            "events/handlers.py перехватывает по job_name='genaos:task_review_pre_pm' "
            "и вызывает task_review.send_task_review() напрямую (Todoist API + ForceReply)."
        ),
    },
    {
        "slot": "workout_today",
        "cron": "30 7 * * *",  # 07:30 CST — утренний план тренировки,  # 23:30 UTC = 07:30 CST — утренний план тренировки
        "prompt": (
            "workout-tracker — обрабатывается напрямую через events/handlers.py "
            "(workout_tracker.send_workout_today, deterministic Python, no LLM)."
        ),
    },
    {
        "slot": "reward_gate_first",
        "cron": "30 22 * * *",  # 22:30 CST — first reward gate,  # 14:30 UTC = 22:30 CST — за 30 мин до final gate
        "prompt": (
            "reward_gate first — обрабатывается напрямую через events/handlers.py "
            "(reward_gate.send_first_gate, deterministic Python)."
        ),
    },
    {
        "slot": "reward_gate_final",
        "cron": "0 23 * * *",  # 23:00 CST — final reward gate,  # 15:00 UTC = 23:00 CST — финал accountability
        "prompt": (
            "reward_gate final — обрабатывается напрямую через events/handlers.py "
            "(reward_gate.send_final_gate, deterministic Python)."
        ),
    },
    {
        "slot": "practice_morning",
        "cron": "0 7 * * *",  # 07:00 CST — утренняя практика,  # 23:00 UTC = 07:00 CST — утренняя практика
        "prompt": (
            "practice_morning — handled by events/handlers.py (presence_practices.send_morning_practice)."
        ),
    },
    {
        "slot": "practice_afternoon",
        "cron": "0 14 * * *",  # 14:00 CST — afternoon dip,  # 06:00 UTC = 14:00 CST — afternoon dip
        "prompt": (
            "practice_afternoon — handled by events/handlers.py (presence_practices.send_afternoon_practice)."
        ),
    },
    {
        "slot": "practice_evening",
        "cron": "0 22 * * *",  # 22:00 CST — pre-sleep,  # 14:00 UTC = 22:00 CST — pre-sleep (before PM at 22:00)
        "prompt": (
            "practice_evening — handled by events/handlers.py (presence_practices.send_evening_practice)."
        ),
    },
    {
        "slot": "food_evening_alert",
        "cron": "10 21 * * *",  # 21:10 CST,  # 13:10 UTC = 21:10 CST — separated from non_negotiables (13:00) and never_miss_twice (13:05)
        "prompt": (
            "food_evening_alert — handled by events/handlers.py (food_alerts.send_food_evening_alert). "
            "Checks today's food/<today>.md vs nutrition_plan.md goals."
        ),
    },
    {
        "slot": "waist_weekly",
        "cron": "0 9 * * 0",  # 09:00 CST вс,  # 01:00 UTC Sunday = 09:00 CST
        "prompt": (
            "waist_weekly — handled by events/handlers.py (body_measurements.send_waist_prompt)."
        ),
    },
    {
        "slot": "measurements_monthly",
        "cron": "0 9 1 * *",  # 09:00 CST 1-го числа,  # 01:00 UTC 1st of month = 09:00 CST
        "prompt": (
            "measurements_monthly — handled by events/handlers.py (body_measurements.send_full_measurements_prompt)."
        ),
    },
    {
        "slot": "whoop_age_weekly",
        "cron": "30 9 * * 0",  # 09:30 CST вс,
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

    # Idempotent re-register: if cron-expression in code changed, update existing job.
    # B-P0-1 fix follow-up: after switching scheduler timezone to Asia/Shanghai,
    # cron-strings in this file became local CST and need to be applied to old DB rows.
    existing_by_name: dict[str, dict] = {row.get("job_name", ""): row for row in existing}

    for spec in _DEFAULT_JOBS:
        job_name = f"{GENAOS_JOB_PREFIX}{spec['slot']}"
        existing_row = existing_by_name.get(job_name)
        if existing_row:
            existing_cron = existing_row.get("cron_expression") or existing_row.get("cron") or ""
            if existing_cron == spec["cron"]:
                logger.info("genaos job already registered, skipping", job_name=job_name)
                continue
            # Cron changed — remove old, re-add with new cron
            try:
                await scheduler.remove_job(job_name)
                logger.info("genaos cron changed, re-registering",
                            job_name=job_name, old=existing_cron, new=spec["cron"])
            except Exception:
                logger.exception("Failed to remove stale genaos job", job_name=job_name)
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
