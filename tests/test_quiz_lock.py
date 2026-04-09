"""Почему: гарантируем, что две параллельные coroutine не запускают
две сессии викторины одновременно для одного (chat_id, topic_id)."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from app.db import Base
from app.models import QuizQuestion, QuizSession
from app.services.quiz import (
    QUIZ_QUESTIONS_COUNT,
    _QUIZ_LOCKS,
    safe_start_quiz,
)


async def _setup_db():
    """Создаёт in-memory БД с достаточным числом вопросов."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        session.add_all([
            QuizQuestion(question=f"Вопрос {i}", answer=f"Ответ {i}")
            for i in range(1, QUIZ_QUESTIONS_COUNT + 2)
        ])
        await session.commit()

    return engine, session_factory


def test_concurrent_start_only_one_session() -> None:
    """Два одновременных вызова safe_start_quiz создают не более одной сессии."""

    async def _run() -> None:
        # Очищаем глобальный словарь замков перед тестом
        _QUIZ_LOCKS.clear()

        engine, session_factory = await _setup_db()
        chat_id, topic_id = 1, 100

        results: list[tuple] = []

        async def _try_start():
            async with session_factory() as session:
                result = await safe_start_quiz(session, chat_id, topic_id)
                results.append(result)

        # Запускаем две корутины параллельно
        await asyncio.gather(_try_start(), _try_start())

        # Ровно одна должна завершиться успехом
        successes = [(s, r) for s, r in results if s is not None]
        failures = [(s, r) for s, r in results if s is None]
        assert len(successes) == 1, (
            f"Ожидалась ровно 1 успешная сессия, получено {len(successes)}"
        )
        assert len(failures) == 1, (
            f"Ожидался ровно 1 отказ, получено {len(failures)}"
        )

        # Проверяем, что в БД тоже только одна активная сессия
        async with session_factory() as session:
            result = await session.execute(
                select(QuizSession).where(
                    QuizSession.chat_id == chat_id,
                    QuizSession.topic_id == topic_id,
                    QuizSession.is_active.is_(True),
                )
            )
            active = result.scalars().all()
            assert len(active) == 1, (
                f"Ожидалась 1 активная сессия в БД, получено {len(active)}"
            )

        await engine.dispose()
        _QUIZ_LOCKS.clear()

    asyncio.run(_run())


def test_different_topics_independent_locks() -> None:
    """Разные топики имеют независимые замки и могут стартовать одновременно."""

    async def _run() -> None:
        _QUIZ_LOCKS.clear()

        engine, session_factory = await _setup_db()

        # Для двух топиков запускаем параллельно — оба должны успеть
        async def _try_start(topic_id: int):
            async with session_factory() as session:
                result = await safe_start_quiz(session, chat_id=1, topic_id=topic_id)
                return result

        r1, r2 = await asyncio.gather(_try_start(1), _try_start(2))

        # Оба должны завершиться успехом (разные топики)
        assert r1[0] is not None, "Топик 1 должен стартовать успешно"
        assert r2[0] is not None, "Топик 2 должен стартовать успешно"

        await engine.dispose()
        _QUIZ_LOCKS.clear()

    asyncio.run(_run())


def test_lock_created_on_demand() -> None:
    """Замок создаётся при первом обращении к (chat_id, topic_id)."""
    from app.services.quiz import _get_quiz_lock
    _QUIZ_LOCKS.clear()

    lock = _get_quiz_lock(10, 20)
    assert lock is not None
    assert (10, 20) in _QUIZ_LOCKS

    # Повторный вызов возвращает тот же объект
    lock2 = _get_quiz_lock(10, 20)
    assert lock is lock2

    _QUIZ_LOCKS.clear()
