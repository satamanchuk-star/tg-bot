"""Почему: статистика тем нужна для ежедневной сводки."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TopicStat


async def bump_topic_stat(
    session: AsyncSession,
    chat_id: int,
    topic_id: int,
    date_key: str,
    last_message: str | None,
) -> None:
    stat = await session.scalar(
        select(TopicStat).where(
            TopicStat.chat_id == chat_id,
            TopicStat.topic_id == topic_id,
            TopicStat.date_key == date_key,
        )
    )
    if stat is None:
        stat = TopicStat(
            chat_id=chat_id,
            topic_id=topic_id,
            date_key=date_key,
            messages_count=0,
        )
        session.add(stat)
    stat.messages_count += 1
    if last_message:
        stat.last_message = last_message[:200]
    await session.flush()


async def get_daily_stats(session: AsyncSession, chat_id: int, date_key: str) -> list[TopicStat]:
    return (
        await session.scalars(
            select(TopicStat)
            .where(TopicStat.chat_id == chat_id, TopicStat.date_key == date_key)
            .order_by(TopicStat.messages_count.desc())
        )
    ).all()
