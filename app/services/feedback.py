"""Почему: обратная связь от пользователей улучшает качество ответов ИИ."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiFeedback

logger = logging.getLogger(__name__)


async def save_feedback(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    bot_message_id: int,
    prompt_text: str,
    reply_text: str,
    rating: int,
) -> AiFeedback | None:
    """Сохраняет оценку ответа. Возвращает None если уже оценивалось."""
    # Проверяем, не оценивал ли уже этот пользователь это сообщение
    existing = await session.execute(
        select(AiFeedback).where(
            AiFeedback.chat_id == chat_id,
            AiFeedback.user_id == user_id,
            AiFeedback.bot_message_id == bot_message_id,
        )
    )
    if existing.scalars().first() is not None:
        return None

    fb = AiFeedback(
        chat_id=chat_id,
        user_id=user_id,
        bot_message_id=bot_message_id,
        prompt_text=prompt_text[:1000],
        reply_text=reply_text[:800],
        rating=rating,
    )
    session.add(fb)
    await session.commit()
    return fb


async def get_relevant_feedback(
    session: AsyncSession,
    *,
    chat_id: int,
    limit: int = 5,
) -> list[AiFeedback]:
    """Возвращает последние положительные ответы для обогащения контекста."""
    stmt = (
        select(AiFeedback)
        .where(
            AiFeedback.chat_id == chat_id,
            AiFeedback.rating > 0,
        )
        .order_by(AiFeedback.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def cleanup_old_feedback(session: AsyncSession, *, retention_days: int = 90) -> int:
    """Удаляет старый фидбек."""
    from datetime import timedelta
    from sqlalchemy import delete

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await session.execute(
        delete(AiFeedback).where(AiFeedback.created_at < cutoff)
    )
    await session.commit()
    return int(result.rowcount or 0)
