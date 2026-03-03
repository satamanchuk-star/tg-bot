"""Почему: единая сборка ежедневной сводки упрощает контроль качества и приватности."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageLog, ModerationEvent


@dataclass(slots=True)
class SentimentStats:
    positive: int = 0
    neutral: int = 0
    negative: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.neutral + self.negative

    @property
    def mood_label(self) -> str:
        if self.total == 0:
            return "недостаточно данных"
        neg_ratio = self.negative / self.total
        pos_ratio = self.positive / self.total
        if neg_ratio > 0.3:
            return "напряжённое"
        if pos_ratio > 0.4:
            return "позитивное"
        return "спокойное"

    @property
    def trend_emoji(self) -> str:
        label = self.mood_label
        if label == "позитивное":
            return "😊"
        if label == "напряжённое":
            return "😤"
        return "😐"


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
    sentiment: SentimentStats = field(default_factory=SentimentStats)


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
            select(MessageLog.text, MessageLog.user_id, MessageLog.sentiment)
            .where(and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since))
            .limit(2000)
        )
    ).all()
    word_counter: Counter[str] = Counter()
    tagged_counter: Counter[int] = Counter()
    sentiment_stats = SentimentStats()
    for text, user_id, sentiment in text_rows:
        if isinstance(user_id, int):
            tagged_counter[user_id] += 1
        # Агрегируем sentiment
        if sentiment == "positive":
            sentiment_stats.positive += 1
        elif sentiment == "negative":
            sentiment_stats.negative += 1
        elif sentiment == "neutral":
            sentiment_stats.neutral += 1
        if not text:
            continue
        for word in text.lower().split():
            cleaned = word.strip(".,!?()[]{}\"'`""«»")
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

    # Настроение определяем по sentiment, а не только по конфликтам
    mood = sentiment_stats.mood_label
    if conflicts > 0 and mood == "спокойное":
        mood = "напряжённое"
    positive_text = "Участники активно помогали друг другу в обсуждениях."

    return DailySummary(
        messages=msg_count,
        active_users=active_users,
        warnings=warnings,
        deletions=deletions,
        strikes=strikes,
        conflicts=conflicts,
        topics=topics,
        mood=mood,
        positive=positive_text,
        top_words=[word for word, _ in word_counter.most_common(8)],
        top_tagged_users=[uid for uid, _ in tagged_counter.most_common(5)],
        sentiment=sentiment_stats,
    )


def build_ai_summary_context(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "нет выделенных тем"
    words = ", ".join(summary.top_words) if summary.top_words else "недостаточно данных"
    tagged = ", ".join(str(uid) for uid in summary.top_tagged_users) if summary.top_tagged_users else "н/д"
    sentiment_line = (
        f"позитивных={summary.sentiment.positive}, "
        f"нейтральных={summary.sentiment.neutral}, "
        f"негативных={summary.sentiment.negative}"
    )
    return (
        "Контекст за последние 24 часа:\n"
        f"- Сообщений: {summary.messages}\n"
        f"- Активных пользователей: {summary.active_users}\n"
        f"- Предупреждений: {summary.warnings}\n"
        f"- Удалений: {summary.deletions}\n"
        f"- Страйков: {summary.strikes}\n"
        f"- Конфликтных часов: {summary.conflicts}\n"
        f"- Тональность сообщений: {sentiment_line}\n"
        f"- Основные темы: {topics}\n"
        f"- Топ слов: {words}\n"
        f"- Самые активные пользователи (id): {tagged}\n"
        "Сформируй короткое резюме для админов, включая оценку настроения чата."
    )


def render_daily_summary(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "темы не выделились"
    heat = "Было пару горячих моментов, но всё спокойно." if summary.conflicts else "День прошёл ровно и спокойно."

    # Sentiment-строка
    s = summary.sentiment
    sentiment_line = ""
    if s.total > 0:
        sentiment_line = (
            f"\n• Настроение чата: {s.mood_label} {s.trend_emoji} "
            f"(+{s.positive} / ~{s.neutral} / -{s.negative})"
        )

    return (
        "Статистика за день:\n"
        f"• Сообщений: {summary.messages}\n"
        f"• Активных соседей: {summary.active_users}\n"
        f"• Предупреждений: {summary.warnings}, удалений: {summary.deletions}, страйков: {summary.strikes}\n"
        f"• Часто обсуждали: {topics}\n"
        f"• Общий фон: {summary.mood}"
        f"{sentiment_line}\n"
        f"• Комментарий: {heat}"
    )
