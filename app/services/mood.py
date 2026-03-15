"""Почему: бот адаптирует тон ответа под текущее настроение чата."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageLog

logger = logging.getLogger(__name__)


class ChatMood(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    TENSE = "tense"
    ANGRY = "angry"


@dataclass(slots=True)
class MoodSnapshot:
    mood: ChatMood
    positive_pct: float  # 0..1
    negative_pct: float  # 0..1
    total_messages: int


# In-memory кэш последних sentiment'ов по топикам
# Ключ: (chat_id, topic_id), значение: deque с парами (sentiment, timestamp)
_MOOD_BUFFER: dict[tuple[int, int | None], deque[tuple[str, datetime]]] = {}
_BUFFER_MAX_SIZE = 50


def record_sentiment(chat_id: int, topic_id: int | None, sentiment: str) -> None:
    """Записывает sentiment сообщения в буфер (вызывать из модерации)."""
    if sentiment not in ("positive", "neutral", "negative"):
        return
    key = (chat_id, topic_id)
    buf = _MOOD_BUFFER.setdefault(key, deque(maxlen=_BUFFER_MAX_SIZE))
    buf.append((sentiment, datetime.now(timezone.utc)))


def get_mood(chat_id: int, topic_id: int | None = None) -> MoodSnapshot:
    """Возвращает текущее настроение чата на основе буфера."""
    key = (chat_id, topic_id)
    buf = _MOOD_BUFFER.get(key)
    if not buf or len(buf) < 3:
        return MoodSnapshot(mood=ChatMood.NEUTRAL, positive_pct=0.0, negative_pct=0.0, total_messages=0)

    total = len(buf)
    positive = sum(1 for s, _ in buf if s == "positive")
    negative = sum(1 for s, _ in buf if s == "negative")
    pos_pct = positive / total
    neg_pct = negative / total

    if neg_pct >= 0.5:
        mood = ChatMood.ANGRY
    elif neg_pct >= 0.3:
        mood = ChatMood.TENSE
    elif pos_pct >= 0.5:
        mood = ChatMood.POSITIVE
    else:
        mood = ChatMood.NEUTRAL

    return MoodSnapshot(mood=mood, positive_pct=pos_pct, negative_pct=neg_pct, total_messages=total)


def get_mood_style_hint(mood: ChatMood) -> str:
    """Возвращает стилевую подсказку для системного промпта на основе настроения."""
    if mood == ChatMood.ANGRY:
        return (
            "\n[Настроение чата: напряжённое. Будь мягче, используй юмор для разрядки, "
            "не обостряй, предложи мирный тон. Начни с понимания.]"
        )
    if mood == ChatMood.TENSE:
        return (
            "\n[Настроение чата: чуть напряжённое. Отвечай спокойно и дружелюбно, "
            "можешь добавить лёгкую шутку для разрядки.]"
        )
    if mood == ChatMood.POSITIVE:
        return (
            "\n[Настроение чата: весёлое! Можешь шутить активнее, "
            "предложить игру или подкинуть интересный факт.]"
        )
    return ""


async def load_mood_from_db(
    session: AsyncSession,
    chat_id: int,
    topic_id: int | None = None,
    *,
    limit: int = 50,
) -> MoodSnapshot:
    """Загружает mood из БД (для холодного старта, когда буфер пуст)."""
    stmt = (
        select(MessageLog.sentiment)
        .where(MessageLog.chat_id == chat_id)
        .order_by(MessageLog.created_at.desc())
        .limit(limit)
    )
    if topic_id is not None:
        stmt = stmt.where(MessageLog.topic_id == topic_id)
    result = await session.execute(stmt)
    sentiments = [row[0] for row in result.all() if row[0]]

    if len(sentiments) < 3:
        return MoodSnapshot(mood=ChatMood.NEUTRAL, positive_pct=0.0, negative_pct=0.0, total_messages=0)

    total = len(sentiments)
    positive = sentiments.count("positive")
    negative = sentiments.count("negative")
    pos_pct = positive / total
    neg_pct = negative / total

    if neg_pct >= 0.5:
        mood = ChatMood.ANGRY
    elif neg_pct >= 0.3:
        mood = ChatMood.TENSE
    elif pos_pct >= 0.5:
        mood = ChatMood.POSITIVE
    else:
        mood = ChatMood.NEUTRAL

    return MoodSnapshot(mood=mood, positive_pct=pos_pct, negative_pct=neg_pct, total_messages=total)
