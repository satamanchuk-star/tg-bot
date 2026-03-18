"""Почему: бот обучается на коррекциях от жителей — если бота поправили, он запоминает."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Паттерны коррекции: пользователь поправляет бота
_CORRECTION_PATTERNS = [
    re.compile(r"(?:нет|не)\s*,?\s*(?:это|там|тут|на самом деле|правильно)", re.I),
    re.compile(r"(?:не\s+так|неправильно|ошибка|ошибаешься|ты\s+не\s+прав)", re.I),
    re.compile(r"(?:а\s+на\s+самом\s+деле|на\s+самом\s+деле|вообще-то)", re.I),
    re.compile(r"(?:уже\s+не\s+работает|уже\s+закрыл|уже\s+переехал)", re.I),
    re.compile(r"(?:номер\s+(?:сменил|изменил|теперь)|адрес\s+(?:другой|изменился))", re.I),
    re.compile(r",\s*а\s+не\s+(?:на|в|по|у)\b", re.I),  # "на Сухановской, а не на Лесной"
]


def is_likely_correction(user_text: str, bot_reply: str) -> bool:
    """Быстрая проверка: похоже ли сообщение на коррекцию бота."""
    if len(user_text) < 10:
        return False
    return any(p.search(user_text) for p in _CORRECTION_PATTERNS)


async def detect_and_apply_correction(
    session: "AsyncSession",
    *,
    chat_id: int,
    user_id: int,
    user_text: str,
    bot_reply: str,
) -> bool:
    """Определяет коррекцию и сохраняет в RAG. Возвращает True если коррекция применена."""
    if not is_likely_correction(user_text, bot_reply):
        return False

    # Пробуем извлечь факт через AI
    from app.services.ai_module import get_ai_client
    ai_client = get_ai_client()
    try:
        prompt = (
            f"Бот ответил: {bot_reply[:300]}\n"
            f"Пользователь поправил: {user_text[:300]}\n"
            "Извлеки правильную информацию. Если это действительно коррекция — "
            'верни JSON: {"is_correction": true, "fact": "правильная информация"}\n'
            'Если это не коррекция — верни: {"is_correction": false}'
        )
        raw = await ai_client.extract_user_facts(prompt, chat_id=chat_id)
        data = json.loads(raw)
        if not data.get("is_correction"):
            return False
        fact = str(data.get("fact", ""))
        if not fact or len(fact) < 5:
            return False
    except Exception:
        logger.warning("Не удалось извлечь коррекцию через AI, пропуск.")
        return False

    # Сохраняем в RAG как community correction
    from app.services.rag import add_rag_message
    corrected_text = f"[Коррекция от жителя] {fact}"
    await add_rag_message(
        session,
        chat_id=chat_id,
        message_text=corrected_text,
        added_by_user_id=user_id,
        source_user_id=user_id,
        ttl_days=180,  # Коррекции живут дольше обычных RAG
    )
    await session.commit()
    logger.info("LEARNING: коррекция применена от user_id=%s: %s", user_id, fact[:100])
    return True
