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
    top_words: list[str]
    top_tagged_users: list[int]


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

    text_rows = (
        await session.execute(
            select(MessageLog.text, MessageLog.user_id)
            .where(and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since))
            .limit(2000)
        )
    ).all()
    word_counter: Counter[str] = Counter()
    tagged_counter: Counter[int] = Counter()
    for text, user_id in text_rows:
        if isinstance(user_id, int):
            tagged_counter[user_id] += 1
        if not text:
            continue
        for word in text.lower().split():
            cleaned = word.strip(".,!?()[]{}\"'`“”«»")
            if len(cleaned) < 4:
                continue
            if cleaned.startswith("http"):
                continue
            word_counter[cleaned] += 1

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
        top_words=[word for word, _ in word_counter.most_common(8)],
        top_tagged_users=[uid for uid, _ in tagged_counter.most_common(5)],
    )


def build_ai_summary_context(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "нет выделенных тем"
    words = ", ".join(summary.top_words) if summary.top_words else "недостаточно данных"
    tagged = ", ".join(str(uid) for uid in summary.top_tagged_users) if summary.top_tagged_users else "н/д"
    return (
        "Контекст за последние 24 часа:\n"
        f"- Сообщений: {summary.messages}\n"
        f"- Активных пользователей: {summary.active_users}\n"
        f"- Предупреждений: {summary.warnings}\n"
        f"- Удалений: {summary.deletions}\n"
        f"- Страйков: {summary.strikes}\n"
        f"- Конфликтных часов: {summary.conflicts}\n"
        f"- Основные темы: {topics}\n"
        f"- Топ слов: {words}\n"
        f"- Самые активные пользователи (id): {tagged}\n"
        "Сформируй короткое резюме для админов."
    )


def render_daily_summary(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "темы не выделились"
    heat = "Было пару горячих моментов, но всё спокойно." if summary.conflicts else "День прошёл ровно и спокойно."
    return (
        "Статистика за день:\n"
        f"• Сообщений: {summary.messages}\n"
        f"• Активных соседей: {summary.active_users}\n"
        f"• Предупреждений: {summary.warnings}, удалений: {summary.deletions}, страйков: {summary.strikes}\n"
        f"• Часто обсуждали: {topics}\n"
        f"• Общий фон: {summary.mood}\n"
        f"• Комментарий: {heat}"
    )
