"""Почему: персистентная история диалогов — бот помнит контекст между рестартами."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatHistory

logger = logging.getLogger(__name__)

# Промпт для структурированного сжатия истории
STRUCTURED_SUMMARY_PROMPT = (
    "Проанализируй диалог и верни JSON:\n"
    "{\n"
    '  "resolved": ["тема1: краткое решение", "тема2: что выяснили"],\n'
    '  "pending": ["вопрос, на который не ответили"],\n'
    '  "user_style": "краткое описание стиля общения пользователя",\n'
    '  "key_facts": ["важный факт из диалога"]\n'
    "}\n"
    "Максимум 3 элемента в каждом массиве. Только факты из диалога."
)

# Сколько записей на пару (chat_id, user_id) держим в БД
HISTORY_LIMIT = 30
# Старше 7 дней — кандидаты на сжатие в саммари
SUMMARY_AGE_DAYS = 7
# Порог для запуска сжатия (когда достигнуто столько обычных записей)
COMPRESS_THRESHOLD = 24
# Сколько записей сжимаем за раз
COMPRESS_BATCH = 10


async def load_context(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    *,
    limit: int = HISTORY_LIMIT,
) -> list[str]:
    """Загружает историю диалога для подстановки в промпт ИИ."""
    stmt = (
        select(ChatHistory)
        .where(ChatHistory.chat_id == chat_id, ChatHistory.user_id == user_id)
        .order_by(ChatHistory.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    lines: list[str] = []
    for r in rows:
        if r.role == "structured_summary" and r.message:
            # Разворачиваем структурированный summary в читаемые подсказки
            try:
                data = json.loads(r.message)
                resolved = data.get("resolved", [])
                pending = data.get("pending", [])
                if resolved:
                    lines.append(f"summary: Уже обсуждали: {'; '.join(resolved[:3])}")
                if pending:
                    lines.append(f"summary: Нерешённый вопрос: {'; '.join(pending[:3])}")
            except (json.JSONDecodeError, AttributeError):
                lines.append(f"{r.role}: {r.text}")
        else:
            lines.append(f"{r.role}: {r.text}")
    return lines


async def save_exchange(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    prompt: str,
    reply: str,
) -> None:
    """Сохраняет пару user/assistant в БД и удаляет самые старые, если превышен лимит."""
    session.add(ChatHistory(
        chat_id=chat_id, user_id=user_id,
        role="user", text=prompt[:1000], message=prompt[:1000],
    ))
    session.add(ChatHistory(
        chat_id=chat_id, user_id=user_id,
        role="assistant", text=reply[:800], message=reply[:800],
    ))
    await session.flush()

    # Проверяем лимит
    count_stmt = (
        select(func.count())
        .select_from(ChatHistory)
        .where(ChatHistory.chat_id == chat_id, ChatHistory.user_id == user_id)
    )
    total = (await session.execute(count_stmt)).scalar() or 0

    if total > HISTORY_LIMIT:
        # Удаляем самые старые записи (не summary), чтобы не превышать лимит
        excess = total - HISTORY_LIMIT
        oldest_ids_stmt = (
            select(ChatHistory.id)
            .where(
                ChatHistory.chat_id == chat_id,
                ChatHistory.user_id == user_id,
                ChatHistory.is_summary == False,  # noqa: E712
            )
            .order_by(ChatHistory.created_at.asc())
            .limit(excess)
        )
        oldest = (await session.execute(oldest_ids_stmt)).scalars().all()
        if oldest:
            await session.execute(
                delete(ChatHistory).where(ChatHistory.id.in_(oldest))
            )

    await session.commit()


async def get_messages_for_compression(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
) -> list[ChatHistory] | None:
    """Возвращает записи для сжатия, если порог достигнут, иначе None."""
    non_summary_count_stmt = (
        select(func.count())
        .select_from(ChatHistory)
        .where(
            ChatHistory.chat_id == chat_id,
            ChatHistory.user_id == user_id,
            ChatHistory.is_summary == False,  # noqa: E712
        )
    )
    count = (await session.execute(non_summary_count_stmt)).scalar() or 0

    if count < COMPRESS_THRESHOLD:
        return None

    # Берём COMPRESS_BATCH самых старых не-summary записей
    oldest_stmt = (
        select(ChatHistory)
        .where(
            ChatHistory.chat_id == chat_id,
            ChatHistory.user_id == user_id,
            ChatHistory.is_summary == False,  # noqa: E712
        )
        .order_by(ChatHistory.created_at.asc())
        .limit(COMPRESS_BATCH)
    )
    result = await session.execute(oldest_stmt)
    return list(result.scalars().all())


async def replace_with_summary(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    old_ids: list[int],
    summary_text: str,
) -> None:
    """Заменяет старые записи одним саммари."""
    if old_ids:
        await session.execute(
            delete(ChatHistory).where(ChatHistory.id.in_(old_ids))
        )
    session.add(ChatHistory(
        chat_id=chat_id,
        user_id=user_id,
        role="summary",
        text=summary_text[:800], message=summary_text[:800],
        is_summary=True,
    ))
    await session.commit()


async def replace_with_structured_summary(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    old_ids: list[int],
    summary_json: str,
) -> None:
    """Заменяет старые записи структурированным JSON-саммари.

    Fallback на обычный текстовый summary при невалидном JSON.
    """
    if old_ids:
        await session.execute(
            delete(ChatHistory).where(ChatHistory.id.in_(old_ids))
        )
    # Проверяем, что JSON валидный — если нет, сохраняем как обычный summary
    try:
        json.loads(summary_json)
        role = "structured_summary"
        is_summary = True
    except (json.JSONDecodeError, ValueError):
        role = "summary"
        is_summary = True

    # Текстовое поле — краткая версия для обратной совместимости
    try:
        data = json.loads(summary_json)
        resolved = data.get("resolved", [])
        text_preview = "Обсуждали: " + "; ".join(resolved[:2]) if resolved else summary_json[:200]
    except Exception:
        text_preview = summary_json[:800]

    session.add(ChatHistory(
        chat_id=chat_id,
        user_id=user_id,
        role=role,
        text=text_preview[:800],
        message=summary_json[:2000],
        is_summary=is_summary,
    ))
    await session.commit()


async def cleanup_old_history(session: AsyncSession, *, retention_days: int = 30) -> int:
    """Удаляет историю старше retention_days дней."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await session.execute(
        delete(ChatHistory).where(ChatHistory.created_at < cutoff)
    )
    await session.commit()
    return int(result.rowcount or 0)
