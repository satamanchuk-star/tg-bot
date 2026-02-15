"""Почему: единая сборка ежедневной сводки упрощает контроль качества и приватности."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageLog, ModerationEvent


@dataclass(slots=True)
class DailySummary:
    messages: int
    active_users: int
    warnings: int
    deletions: int
    strikes: int
    conflicts: int
    topics: list[str]
    mood: str
    positive: str


async def build_daily_summary(session: AsyncSession, chat_id: int) -> DailySummary:
    since = datetime.utcnow() - timedelta(days=1)
    msg_count = int(
        await session.scalar(
            select(func.count()).select_from(MessageLog).where(
                and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since)
            )
        )
        or 0
    )
    active_users = int(
        await session.scalar(
            select(func.count(func.distinct(MessageLog.user_id))).where(
                and_(
                    MessageLog.chat_id == chat_id,
                    MessageLog.created_at >= since,
                )
            )
        )
        or 0
    )

    events = (
        await session.execute(
            select(ModerationEvent).where(
                and_(ModerationEvent.chat_id == chat_id, ModerationEvent.created_at >= since)
            )
        )
    ).scalars().all()

    warnings = sum(1 for item in events if item.event_type == "warn")
    deletions = sum(1 for item in events if item.event_type == "delete")
    strikes = sum(1 for item in events if item.event_type == "strike")

    topic_rows = (
        await session.execute(
            select(MessageLog.topic_id, func.count(MessageLog.id))
            .where(and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since))
            .group_by(MessageLog.topic_id)
            .order_by(func.count(MessageLog.id).desc())
            .limit(3)
        )
    ).all()
    topics = [f"тема {row[0]} ({row[1]} сообщений)" for row in topic_rows if row[0] is not None]

    conflict_buckets: dict[int, set[int]] = defaultdict(set)
    for item in events:
        if item.severity >= 2:
            hour_key = int(item.created_at.timestamp() // 3600)
            conflict_buckets[hour_key].add(item.user_id)
    conflicts = sum(1 for users in conflict_buckets.values() if len(users) >= 2)

    mood = "спокойное" if conflicts == 0 else "напряжённое"
    positive = "Участники активно помогали друг другу в обсуждениях."

    return DailySummary(
        messages=msg_count,
        active_users=active_users,
        warnings=warnings,
        deletions=deletions,
        strikes=strikes,
        conflicts=conflicts,
        topics=topics,
        mood=mood,
        positive=positive,
    )


def render_daily_summary(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "темы не выделены"
    return (
        "Ежедневная сводка:\n"
        f"• Сообщений: {summary.messages}\n"
        f"• Активных участников: {summary.active_users}\n"
        f"• Предупреждений: {summary.warnings}\n"
        f"• Удалений: {summary.deletions}\n"
        f"• Страйков: {summary.strikes}\n"
        f"• Конфликтов: {summary.conflicts}\n"
        f"• Основные темы: {topics}\n"
        f"• Настроение дня: {summary.mood}\n"
        f"• Позитив: {summary.positive}"
    )
