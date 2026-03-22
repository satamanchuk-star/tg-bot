"""Почему: удерживаем размер SQLite под контролем без изменения бизнес-логики модулей."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, text
from sqlalchemy.sql.dml import Delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AiFeedback, AiUsage, ChatHistory, FrequentQuestion, MessageLog, ModerationEvent, RagMessage, TopicStat



async def cleanup_old_data(session: AsyncSession, *, now_utc: datetime | None = None) -> dict[str, int]:
    """Удаляет устаревшие записи из сервисных таблиц.

    Храним только окна, реально нужные для ежедневной аналитики и диагностики.
    """
    now = now_utc or datetime.now(timezone.utc)
    logs_cutoff = now - timedelta(days=max(1, settings.db_logs_retention_days))
    stats_cutoff = now - timedelta(days=max(1, settings.db_stats_retention_days))
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
    # Очистка истёкших RAG-записей
    removed_rag = await _delete_and_count(
        session,
        delete(RagMessage).where(
            RagMessage.expires_at.isnot(None),
            RagMessage.expires_at < now,
        ),
    )
    # Очистка старой истории диалогов (>30 дней)
    history_cutoff = now - timedelta(days=30)
    removed_chat_history = await _delete_and_count(
        session,
        delete(ChatHistory).where(ChatHistory.created_at < history_cutoff),
    )
    # Очистка старого feedback (>90 дней)
    feedback_cutoff = now - timedelta(days=90)
    removed_feedback = await _delete_and_count(
        session,
        delete(AiFeedback).where(AiFeedback.created_at < feedback_cutoff),
    )
    # Очистка устаревших FAQ (не спрашивали >90 дней)
    removed_faq = await _delete_and_count(
        session,
        delete(FrequentQuestion).where(FrequentQuestion.last_asked_at < feedback_cutoff),
    )

    await session.commit()

    return {
        "message_logs": removed_message_logs,
        "moderation_events": removed_moderation_events,
        "topic_stats": removed_topic_stats,
        "ai_usage": removed_ai_usage,
        "rag_expired": removed_rag,
        "chat_history": removed_chat_history,
        "ai_feedback": removed_feedback,
        "frequent_questions": removed_faq,
    }


async def optimize_sqlite(session: AsyncSession) -> None:
    """Выполняет щадящую оптимизацию SQLite после очистки."""
    if not settings.database_url.startswith("sqlite+"):
        return
    await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
    await session.execute(text("PRAGMA incremental_vacuum(2000);"))
    await session.commit()


async def _delete_and_count(session: AsyncSession, query: Delete) -> int:
    result = await session.execute(query)
    return int(result.rowcount or 0)
