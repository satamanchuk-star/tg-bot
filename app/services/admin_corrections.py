"""Почему: администраторы могут исправлять ответы бота — правка вносится
в канонический RAG бессрочно и сбрасывает кэш похожих запросов.

Отличие от learning.py (коррекции жителей):
- is_admin=True → запись постоянная (без срока), вытесняет конфликтующие записи
- Кэш AI-ответов инвалидируется по ключевым словам
- Более широкий набор паттернов (включая явные команды "запомни", "исправь" и т.п.)
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Паттерны коррекции от администратора (строгий приоритет над learning.py)
_ADMIN_CORRECTION_PATTERNS: list[re.Pattern[str]] = [
    # Явные команды запомнить/зафиксировать
    re.compile(r"\bзапомни\b", re.I),
    re.compile(r"\bзапиши\b", re.I),
    re.compile(r"\bзафиксируй\b", re.I),
    re.compile(r"\bисправь\b", re.I),
    re.compile(r"\bактуализируй\b", re.I),
    re.compile(r"\bобнови\b.{0,20}\bбаз", re.I),
    re.compile(r"\bвнеси\s+в\s+базу\b", re.I),
    # Указание на верную информацию
    re.compile(r"\bверн\w*\s+(?:ответ|информац|данн)", re.I),
    re.compile(r"\bправильн\w*\s+(?:ответ|информац|данн)", re.I),
    re.compile(r"\bвот\s+верн\w", re.I),
    re.compile(r"\bвот\s+правильн\w", re.I),
    # Отрицание + указание на ошибку
    re.compile(r"(?:ты\s+)?не\s+прав\b", re.I),
    re.compile(r"\bневерно\b", re.I),
    re.compile(r"\bнеправильно\b", re.I),
    re.compile(r"\bошибаешься\b", re.I),
    re.compile(r"\bошибка\b.*\bбота?\b", re.I),
    # На самом деле / вообще-то
    re.compile(r"\bна\s+самом\s+деле\b", re.I),
    re.compile(r"\bвообще-то\b", re.I),
    # Из learning.py — изменение контактов/адресов/расписания
    re.compile(r"(?:нет|не)\s*,?\s*(?:это|там|тут|на самом деле|правильно)", re.I),
    re.compile(r"(?:не\s+так|неправильно|ошибка|ошибаешься|ты\s+не\s+прав)", re.I),
    re.compile(r"(?:уже\s+не\s+работает|уже\s+закрыл|уже\s+переехал|уже\s+не\s+так)", re.I),
    re.compile(r"(?:номер\s+(?:сменил|изменил|теперь)|адрес\s+(?:другой|изменился))", re.I),
    re.compile(r",\s*а\s+не\s+(?:на|в|по|у)\b", re.I),
    re.compile(r"(?:телефон|номер)\s+(?:поменяли|поменялся|сменили|другой|новый|теперь)", re.I),
    re.compile(r"(?:адрес|расположение)\s+(?:поменяли|поменялся|сменили|другой|теперь)", re.I),
    re.compile(r"(?:расписание|график|время)\s+(?:поменяли|поменялось|изменили|другое|теперь)", re.I),
    re.compile(r"не\s+\d[\d\-\s]+,?\s*а\s+\d", re.I),
    re.compile(r"(?:там\s+уже\s+другой|это\s+устарело|это\s+старая\s+информация)", re.I),
    re.compile(r"(?:давно\s+закрыли|давно\s+переехали|больше\s+не\s+работает)", re.I),
    re.compile(r"(?:сейчас\s+(?:уже|там)\s+(?:другой|другая|другое|нет))", re.I),
    re.compile(r"(?:правильн\w+\s+(?:будет|ответ|номер|адрес|название))", re.I),
    re.compile(r"(?:точн\w+\s+(?:адрес|номер|название|телефон))", re.I),
]


def is_admin_correction(text: str) -> bool:
    """Быстрая проверка: похоже ли сообщение на коррекцию от администратора."""
    if len(text) < 8:
        return False
    return any(p.search(text) for p in _ADMIN_CORRECTION_PATTERNS)


async def apply_admin_correction(
    session: "AsyncSession",
    *,
    chat_id: int,
    admin_id: int,
    admin_text: str,
    bot_reply: str,
) -> tuple[bool, str]:
    """Обрабатывает коррекцию от администратора.

    1. Извлекает правильный факт + ключевые слова через AI
    2. Сохраняет в RAG бессрочно (is_admin=True)
    3. Инвалидирует кэш AI-ответов по ключевым словам

    Возвращает (success, extracted_fact).
    """
    from app.services.ai_module import get_ai_client, invalidate_cache_by_keywords
    from app.services.rag import add_rag_message

    ai_client = get_ai_client()

    # 1. Извлекаем правильный факт через AI
    extraction_prompt = (
        f"Бот ответил: {bot_reply[:400]}\n"
        f"Администратор поправил: {admin_text[:400]}\n\n"
        "Это официальная поправка от администратора жилого комплекса. Извлеки:\n"
        "1. Правильный факт/ответ (поле 'fact') — чётко и конкретно\n"
        "2. Ключевые слова для инвалидации кэша (поле 'keywords') — 3-7 русских слов\n\n"
        'Верни только JSON: {"fact": "...", "keywords": ["слово1", "слово2", ...]}\n'
        "Если поправить нечего — верни: {\"fact\": \"\"}"
    )

    fact: str = ""
    keywords: list[str] = []
    try:
        raw = await ai_client.extract_user_facts(extraction_prompt, chat_id=chat_id)
        # Чистим markdown-обёртку если модель добавила ```json ... ```
        clean = raw.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```[a-z]*\n?", "", clean)
            clean = re.sub(r"\n?```$", "", clean)
        data = json.loads(clean)
        fact = str(data.get("fact", "")).strip()
        keywords = [str(k) for k in data.get("keywords", []) if k]
    except Exception:
        logger.warning("ADMIN_CORRECTION: не удалось извлечь факт через AI.")
        return False, ""

    if not fact or len(fact) < 5:
        logger.info("ADMIN_CORRECTION: AI не нашёл факт в коррекции от admin_id=%s.", admin_id)
        return False, ""

    # 2. Сохраняем в RAG как постоянную запись администратора
    corrected_text = f"[Поправка администратора] {fact}"
    await add_rag_message(
        session,
        chat_id=chat_id,
        message_text=corrected_text,
        added_by_user_id=admin_id,
        source_user_id=admin_id,
        is_admin=True,  # бессрочно, вытесняет конфликтующие записи
    )
    await session.commit()
    logger.info(
        "ADMIN_CORRECTION: применена от admin_id=%s, chat_id=%s: %s",
        admin_id, chat_id, fact[:120],
    )

    # 3. Инвалидируем кэш AI-ответов
    if keywords:
        invalidated = invalidate_cache_by_keywords(keywords)
        logger.info(
            "ADMIN_CORRECTION: сброшено %d записей кэша по ключам %s",
            invalidated, keywords,
        )

    return True, fact
