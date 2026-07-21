"""Почему: наполняем таблицу quiz_questions из data/quiz_questions.json.

Синхронизация по паре (question, answer): новые добавляем, исчезнувшие из сида
удаляем, у существующих сохраняем used_at (не сбрасываем историю показов).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizQuestion

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "quiz_questions.json"
SEED_FILE_FALLBACK = Path(__file__).resolve().parent.parent / "kb" / "quiz_questions.json"


def _load_seed() -> list[dict]:
    path = SEED_FILE if SEED_FILE.exists() else SEED_FILE_FALLBACK
    if not path.exists():
        logger.warning("QUIZ seed: файл %s не найден.", path)
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Отбрасываем служебные объекты вида {"error": ...} и пустые пары.
    return [
        d for d in data
        if isinstance(d, dict) and d.get("question") and d.get("answer")
    ]


def _key(question: str, answer: str) -> tuple[str, str]:
    return question.strip().lower(), answer.strip().lower()


async def seed_quiz_questions(session: AsyncSession) -> int:
    """Идемпотентно синхронизирует пул вопросов. Возвращает число вопросов в базе."""
    seed = _load_seed()
    if not seed:
        # Пустой сид не должен обнулять уже загруженную базу.
        return int(await session.scalar(select(func.count()).select_from(QuizQuestion)) or 0)

    seed_by_key = { _key(d["question"], d["answer"]): d for d in seed }

    existing = (await session.execute(select(QuizQuestion))).scalars().all()
    existing_keys = set()
    for row in existing:
        k = _key(row.question, row.answer)
        existing_keys.add(k)
        if k not in seed_by_key:
            await session.delete(row)  # вопрос убрали из сида

    added = 0
    for k, d in seed_by_key.items():
        if k in existing_keys:
            continue
        session.add(QuizQuestion(
            question=d["question"].strip(),
            answer=d["answer"].strip(),
            comment=(str(d.get("comment") or "").strip() or None),
            category=(d.get("category") or None),
        ))
        added += 1

    if added:
        # Появились свежие вопросы — снимаем флаг «база исчерпана», викторина
        # снова начнёт запускаться в 20:00.
        from app.models import MigrationFlag
        flag = await session.get(MigrationFlag, "quiz_bank_exhausted")
        if flag is not None:
            await session.delete(flag)
            logger.info("QUIZ seed: база пополнена (%d) — викторина снова открыта.", added)

    await session.flush()
    total = int(await session.scalar(
        select(func.count()).select_from(QuizQuestion)
    ) or 0)
    logger.info("QUIZ seed: в базе %d вопросов.", total)
    return total
