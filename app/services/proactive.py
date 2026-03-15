"""Почему: проактивные подсказки — бот помогает без прямого обращения, когда точно может."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.ai_module import get_ai_client
from app.services.resident_kb import build_resident_answer

logger = logging.getLogger(__name__)

# Лимиты антиспама: максимум 1 проактивное сообщение в час на топик
_LAST_PROACTIVE: dict[tuple[int, int | None], datetime] = {}
_PROACTIVE_COOLDOWN = timedelta(hours=1)

# Трекер активности топика: (chat_id, topic_id) → список timestamp'ов
_TOPIC_ACTIVITY: dict[tuple[int, int | None], list[datetime]] = defaultdict(list)
_ACTIVITY_WINDOW = timedelta(minutes=10)
_ACTIVITY_THRESHOLD = 5  # Если больше 5 сообщений за 10 минут — не вмешиваемся

# Вопросительные паттерны
_QUESTION_PATTERNS = [
    re.compile(r"\bгде\s+(?:тут|здесь|рядом|ближайш|можно|находит)", re.IGNORECASE),
    re.compile(r"\bкто\s+(?:знает|подскажет|может|сталкивал)", re.IGNORECASE),
    re.compile(r"\bподскажите\b", re.IGNORECASE),
    re.compile(r"\bпосоветуйте\b", re.IGNORECASE),
    re.compile(r"\bкак\s+(?:найти|добраться|попасть|связаться|позвонить)", re.IGNORECASE),
    re.compile(r"\bесть\s+(?:ли|кто-нибудь|у кого)", re.IGNORECASE),
    re.compile(r"\bне\s+знаете\b", re.IGNORECASE),
    re.compile(r"\bкуда\s+(?:обратиться|писать|звонить|идти)", re.IGNORECASE),
    re.compile(r"\bчей\s+(?:это|номер|телефон|машина|авто)", re.IGNORECASE),
    re.compile(r"\bсколько\s+стоит\b", re.IGNORECASE),
]

def _is_question(text: str) -> bool:
    """Определяет, содержит ли сообщение вопрос, на который бот может ответить."""
    if "?" in text:
        return True
    return any(p.search(text) for p in _QUESTION_PATTERNS)


def _is_topic_active(chat_id: int, topic_id: int | None) -> bool:
    """Проверяет, идёт ли активная дискуссия (>5 сообщений за 10 мин)."""
    key = (chat_id, topic_id)
    now = datetime.now(timezone.utc)
    timestamps = _TOPIC_ACTIVITY[key]
    # Очищаем старые
    cutoff = now - _ACTIVITY_WINDOW
    _TOPIC_ACTIVITY[key] = [t for t in timestamps if t > cutoff]
    return len(_TOPIC_ACTIVITY[key]) >= _ACTIVITY_THRESHOLD


def _is_on_cooldown(chat_id: int, topic_id: int | None) -> bool:
    """Проверяет, прошёл ли cooldown для проактивного ответа в этом топике."""
    key = (chat_id, topic_id)
    last = _LAST_PROACTIVE.get(key)
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < _PROACTIVE_COOLDOWN


def _mark_proactive_sent(chat_id: int, topic_id: int | None) -> None:
    """Помечает, что проактивное сообщение было отправлено."""
    _LAST_PROACTIVE[(chat_id, topic_id)] = datetime.now(timezone.utc)


def register_message_activity(chat_id: int, topic_id: int | None) -> None:
    """Регистрирует активность в топике (вызывать из middleware)."""
    key = (chat_id, topic_id)
    _TOPIC_ACTIVITY[key].append(datetime.now(timezone.utc))
    # Не даём списку расти бесконечно
    if len(_TOPIC_ACTIVITY[key]) > 100:
        cutoff = datetime.now(timezone.utc) - _ACTIVITY_WINDOW
        _TOPIC_ACTIVITY[key] = [t for t in _TOPIC_ACTIVITY[key] if t > cutoff]


async def maybe_proactive_reply(message: Message, bot: Bot) -> bool:
    """Проверяет, стоит ли боту проактивно ответить. Возвращает True, если ответил.

    Условия:
    1. Проактивный режим включён
    2. Сообщение содержит вопрос или приветствие новичка
    3. Топик не слишком активный (нет горячей дискуссии)
    4. Прошёл cooldown с прошлого проактивного ответа
    5. Бот имеет релевантную информацию
    """
    if not settings.ai_feature_proactive:
        return False
    if message.chat.id != settings.forum_chat_id:
        return False
    if message.from_user is None or message.from_user.is_bot:
        return False
    text = message.text or message.caption or ""
    if not text or len(text) < 10:
        return False

    chat_id = message.chat.id
    topic_id = message.message_thread_id

    # Cooldown
    if _is_on_cooldown(chat_id, topic_id):
        return False

    # Не вмешиваемся в горячие дискуссии
    if _is_topic_active(chat_id, topic_id):
        return False

    # Вопросительное сообщение — пробуем ответить из базы знаний
    if _is_question(text):
        # Проверяем, есть ли у нас релевантная информация
        answer = build_resident_answer(text)
        if answer:
            # У нас есть точная информация — отвечаем
            try:
                ai_client = get_ai_client()
                reply = await ai_client.assistant_reply(
                    text, context=[], chat_id=chat_id,
                )
                if reply and reply.strip():
                    await message.reply(reply)
                    _mark_proactive_sent(chat_id, topic_id)
                    logger.info("PROACTIVE: question hint sent, topic=%s", topic_id)
                    return True
            except Exception:
                logger.warning("Не удалось сгенерировать проактивный ответ")

    return False
