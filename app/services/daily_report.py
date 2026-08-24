"""Почему: владелец видит работу бота без ковыряния в логах — сводка в лог-чат."""

from __future__ import annotations

import logging

from aiogram import Bot

from app.config import settings
from app.db import get_session

logger = logging.getLogger(__name__)


async def send_daily_report(bot: Bot) -> None:
    """Вечерняя сводка (22:30) в лог-чат: то же, что /аналитика, но за сутки.

    Раньше отчёт показывал только расходы на AI — по нему нельзя было понять,
    играют ли жители и справедливо ли бот отвергает ответы в викторине.
    """
    try:
        from app.services.analytics import build_analytics_report

        async for session in get_session():
            report = await build_analytics_report(session, days=1)
            break
        else:
            return
        await bot.send_message(
            settings.admin_log_chat_id, report, disable_notification=True,
        )
        logger.info("DAILY_REPORT: сводка отправлена.")
    except Exception:
        logger.warning("DAILY_REPORT: не удалось отправить сводку.", exc_info=True)
