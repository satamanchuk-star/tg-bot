"""RAG-сервис: хранение и поиск сообщений из базы знаний чата ЖК.

Администраторы добавляют хорошие сообщения командой /rag_bot (реплай),
бот использует их как контекст при ответах пользователям.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RagMessage

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> set[str]:
    """Возвращает множество слов (≥ 3 символа) из текста."""
    return {w for w in re.findall(r"[а-яёa-z]+", text.lower()) if len(w) >= 3}


async def add_rag_message(
    session: AsyncSession,
    *,
    chat_id: int,
    message_text: str,
    added_by_user_id: int,
    source_user_id: int | None = None,
    source_message_id: int | None = None,
) -> RagMessage:
    """Добавляет сообщение в RAG-базу."""
    record = RagMessage(
        chat_id=chat_id,
        message_text=message_text,
        added_by_user_id=added_by_user_id,
        source_user_id=source_user_id,
        source_message_id=source_message_id,
        created_at=datetime.utcnow(),
    )
    session.add(record)
    await session.flush()
    return record


async def search_rag(
    session: AsyncSession,
    chat_id: int,
    query: str,
    top_k: int = 5,
) -> list[RagMessage]:
    """Ищет наиболее релевантные сообщения из RAG-базы по пересечению слов."""
    if top_k <= 0:
        return []

    messages = await get_all_rag_messages(session, chat_id)
    return rank_rag_messages(messages, query=query, top_k=top_k)


async def get_all_rag_messages(session: AsyncSession, chat_id: int) -> list[RagMessage]:
    """Возвращает все сообщения RAG для чата в хронологическом порядке."""
    result = await session.execute(
        select(RagMessage)
        .where(RagMessage.chat_id == chat_id)
        .order_by(RagMessage.created_at.asc())
    )
    return list(result.scalars().all())


def rank_rag_messages(
    messages: list[RagMessage],
    *,
    query: str,
    top_k: int | None = None,
) -> list[RagMessage]:
    """Сортирует RAG-сообщения: сначала релевантные запросу, затем остальные."""
    query_tokens = _tokenize(query)
    if not messages:
        return []
    if not query_tokens:
        return messages[:top_k] if top_k is not None else messages

    scored: list[tuple[RagMessage, float]] = []
    untouched: list[RagMessage] = []
    for msg in messages:
        msg_tokens = _tokenize(msg.message_text)
        if not msg_tokens:
            untouched.append(msg)
            continue
        overlap = len(query_tokens & msg_tokens)
        if overlap > 0:
            score = overlap / len(query_tokens)
            scored.append((msg, score))
            continue
        untouched.append(msg)

    scored.sort(key=lambda x: x[1], reverse=True)
    ranked = [msg for msg, _ in scored] + untouched
    return ranked[:top_k] if top_k is not None else ranked


def format_rag_context(messages: list[RagMessage]) -> str:
    """Форматирует RAG-сообщения для вставки в промпт ИИ."""
    if not messages:
        return ""
    parts = []
    for i, msg in enumerate(messages, 1):
        parts.append(f"[{i}] {msg.message_text}")
    return "\n".join(parts)


async def get_rag_count(session: AsyncSession, chat_id: int) -> int:
    """Возвращает количество записей в RAG-базе."""
    from sqlalchemy import func
    result = await session.scalar(
        select(func.count()).select_from(RagMessage).where(RagMessage.chat_id == chat_id)
    )
    return int(result or 0)
