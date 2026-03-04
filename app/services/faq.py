"""Почему: автоматический FAQ ускоряет ответы на повторяющиеся вопросы и экономит токены."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FrequentQuestion

logger = logging.getLogger(__name__)

# Минимум обращений для закрепления ответа как «лучшего»
MIN_ASK_COUNT = 3
# Минимум положительных оценок при нулевых отрицательных
MIN_POSITIVE_RATINGS = 2


async def track_question(
    session: AsyncSession,
    *,
    chat_id: int,
    question_key: str,
    answer: str,
) -> FrequentQuestion:
    """Инкрементирует счётчик вопроса или создаёт новую запись."""
    if not question_key:
        fq = FrequentQuestion(
            chat_id=chat_id, question_key="", best_answer=answer, ask_count=1,
        )
        session.add(fq)
        await session.flush()
        return fq

    stmt = select(FrequentQuestion).where(
        FrequentQuestion.chat_id == chat_id,
        FrequentQuestion.question_key == question_key,
    )
    result = await session.execute(stmt)
    fq = result.scalar_one_or_none()

    if fq is None:
        fq = FrequentQuestion(
            chat_id=chat_id,
            question_key=question_key,
            best_answer=answer[:800],
            ask_count=1,
        )
        session.add(fq)
    else:
        fq.ask_count += 1
        fq.last_asked_at = datetime.utcnow()
        # Обновляем ответ, если ещё нет закреплённого лучшего
        if not _is_answer_locked(fq):
            fq.best_answer = answer[:800]

    await session.flush()
    return fq


async def get_faq_answer(
    session: AsyncSession,
    *,
    chat_id: int,
    question_key: str,
) -> str | None:
    """Возвращает закреплённый ответ из FAQ, если вопрос достаточно частый и оценён."""
    if not question_key:
        return None

    stmt = select(FrequentQuestion).where(
        FrequentQuestion.chat_id == chat_id,
        FrequentQuestion.question_key == question_key,
    )
    result = await session.execute(stmt)
    fq = result.scalar_one_or_none()

    if fq is None:
        return None

    if _is_answer_locked(fq):
        return fq.best_answer

    return None


async def update_faq_rating(
    session: AsyncSession,
    *,
    chat_id: int,
    question_key: str,
    delta: int,
) -> None:
    """Обновляет рейтинг FAQ-записи по ключу вопроса."""
    if not question_key:
        return
    stmt = select(FrequentQuestion).where(
        FrequentQuestion.chat_id == chat_id,
        FrequentQuestion.question_key == question_key,
    )
    result = await session.execute(stmt)
    fq = result.scalar_one_or_none()
    if fq is None:
        return

    if delta > 0:
        fq.positive_ratings += 1
    else:
        fq.negative_ratings += 1
        # Если ответ стал плохим — сбрасываем best_answer
        if fq.negative_ratings > fq.positive_ratings and fq.best_answer:
            fq.best_answer = None

    await session.flush()


async def cleanup_stale_faq(session: AsyncSession, *, stale_days: int = 90) -> int:
    """Удаляет FAQ-записи, не спрашиваемые более stale_days дней."""
    cutoff = datetime.utcnow() - timedelta(days=stale_days)
    result = await session.execute(
        delete(FrequentQuestion).where(FrequentQuestion.last_asked_at < cutoff)
    )
    await session.commit()
    return int(result.rowcount or 0)


def _is_answer_locked(fq: FrequentQuestion) -> bool:
    """Ответ «закреплён», если вопрос задавали достаточно часто и оценки положительные."""
    return (
        fq.ask_count >= MIN_ASK_COUNT
        and fq.positive_ratings >= MIN_POSITIVE_RATINGS
        and fq.negative_ratings == 0
        and fq.best_answer is not None
    )
