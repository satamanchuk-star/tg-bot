"""Почему: инкапсулируем работу со страйками для повторного использования."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Strike


STRIKE_RESET_DAYS = 30


async def add_strike(session: AsyncSession, user_id: int, chat_id: int) -> int:
    """Добавляет страйк и возвращает текущее количество страйков."""

    oldest = await session.scalar(
        select(func.min(Strike.created_at)).where(
            Strike.user_id == user_id,
            Strike.chat_id == chat_id,
        )
    )
    now = datetime.utcnow()
    if oldest and now - oldest > timedelta(days=STRIKE_RESET_DAYS):
        await session.execute(
            delete(Strike).where(Strike.user_id == user_id, Strike.chat_id == chat_id)
        )
        oldest = None

    session.add(Strike(user_id=user_id, chat_id=chat_id, created_at=now))
    await session.flush()

    count = await session.scalar(
        select(func.count()).select_from(Strike).where(
            Strike.user_id == user_id,
            Strike.chat_id == chat_id,
        )
    )
    return int(count or 0)


async def clear_strikes(session: AsyncSession, user_id: int, chat_id: int) -> None:
    """Очищает страйки пользователя."""

    await session.execute(
        delete(Strike).where(Strike.user_id == user_id, Strike.chat_id == chat_id)
    )
