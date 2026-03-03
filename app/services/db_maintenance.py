"""Почему: удерживаем размер SQLite под контролем без изменения бизнес-логики модулей."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.dml import Delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AiUsage, MessageLog, ModerationEvent, TopicStat

logger = logging.getLogger(__name__)



async def cleanup_old_data(session: AsyncSession, *, now_utc: datetime | None = None) -> dict[str, int]:
    """Удаляет устаревшие записи из сервисных таблиц.

    Храним только окна, реально нужные для ежедневной аналитики и диагностики.
    """
    now = now_utc or datetime.utcnow()
    logs_cutoff = now - timedelta(days=max(1, settings.db_logs_retention_days))
    stats_cutoff = now - timedelta(days=max(1, settings.db_stats_retention_days))
    logs_cutoff_key = logs_cutoff.date().isoformat()
    stats_cutoff_key = stats_cutoff.date().isoformat()

    removed_message_logs = await _delete_and_count(
        session,
        delete(MessageLog).where(MessageLog.created_at < logs_cutoff),
    )
    removed_moderation_events = await _delete_and_count(
        session,
        delete(ModerationEvent).where(ModerationEvent.created_at < logs_cutoff),
    )
    removed_topic_stats = await _delete_and_count(
        session,
        delete(TopicStat).where(TopicStat.date_key < stats_cutoff_key),
    )
    removed_ai_usage = await _delete_and_count(
        session,
        delete(AiUsage).where(AiUsage.date_key < stats_cutoff_key),
    )

    await session.commit()

    return {
        "message_logs": removed_message_logs,
        "moderation_events": removed_moderation_events,
        "topic_stats": removed_topic_stats,
        "ai_usage": removed_ai_usage,
    }


async def optimize_sqlite(session: AsyncSession) -> None:
    """Выполняет щадящую оптимизацию SQLite после очистки."""
    if not settings.database_url.startswith("sqlite+"):
        return
    try:
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
        await session.execute(text("PRAGMA incremental_vacuum(2000);"))
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.warning("Не удалось выполнить SQLite оптимизацию после очистки.")


async def _delete_and_count(session: AsyncSession, query: Delete) -> int:
    result = await session.execute(query)
    return int(result.rowcount or 0)
