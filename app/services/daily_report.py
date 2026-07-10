"""Почему: владелец видит работу бота без ковыряния в логах — сводка в лог-чат."""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import func, select

from app.config import settings
from app.db import get_session
from app.models import AiTaskLog, UnansweredQuestion
from app.utils.time import now_tz

logger = logging.getLogger(__name__)


async def send_daily_report(bot: Bot) -> None:
    """Вечерняя сводка (22:30): запросы, токены, стоимость, «не знаю», топ-задачи."""
    date_key = now_tz().date().isoformat()
    try:
        async for session in get_session():
            # Агрегаты по AI-задачам за сегодня
            total_row = (await session.execute(
                select(
                    func.count(AiTaskLog.id),
                    func.coalesce(func.sum(AiTaskLog.tokens_used), 0),
                    func.coalesce(func.sum(AiTaskLog.cost_usd), 0.0),
                ).where(AiTaskLog.date_key == date_key)
            )).one()
            requests_n, tokens_n, cost_usd = int(total_row[0]), int(total_row[1]), float(total_row[2])

            by_task = (await session.execute(
                select(AiTaskLog.task, func.count(AiTaskLog.id))
                .where(AiTaskLog.date_key == date_key)
                .group_by(AiTaskLog.task)
                .order_by(func.count(AiTaskLog.id).desc())
                .limit(5)
            )).all()

            open_questions = int(await session.scalar(
                select(func.count(UnansweredQuestion.id)).where(
                    UnansweredQuestion.status == "open"
                )
            ) or 0)
            break
        else:
            return

        if requests_n == 0 and open_questions == 0:
            logger.info("DAILY_REPORT: пропущен — активности не было.")
            return

        lines = [
            f"📊 Сводка за {date_key}",
            f"AI-запросов: {requests_n} · токенов: {tokens_n:,}".replace(",", " ")
            + (f" · ≈${cost_usd:.2f}" if cost_usd else ""),
        ]
        if by_task:
            top = ", ".join(f"{task} ×{n}" for task, n in by_task)
            lines.append(f"По задачам: {top}")
        if open_questions:
            lines.append(
                f"Вопросов без ответа накоплено: {open_questions} "
                "(дайджест — по понедельникам)"
            )
        await bot.send_message(
            settings.admin_log_chat_id, "\n".join(lines), disable_notification=True,
        )
        logger.info("DAILY_REPORT: сводка отправлена.")
    except Exception:
        logger.warning("DAILY_REPORT: не удалось отправить сводку.", exc_info=True)
