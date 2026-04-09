"""Почему: гарантируем, что end_quiz_session НЕ удаляет вопросы из QuizQuestion."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from app.db import Base
from app.models import QuizQuestion, QuizSession, QuizUsedQuestion
from app.services.quiz import (
    end_quiz_session,
    get_available_questions_count,
    get_random_question,
    set_current_question,
    start_quiz_session,
)


def test_end_quiz_session_does_not_delete_questions() -> None:
    """Вопросы должны оставаться в QuizQuestion после завершения сессии."""

    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            # Добавляем вопросы
            questions = [
                QuizQuestion(question=f"Вопрос {i}", answer=f"Ответ {i}")
                for i in range(1, 4)
            ]
            session.add_all(questions)
            await session.commit()

            # Запускаем сессию и помечаем вопросы использованными
            quiz_session = await start_quiz_session(session, chat_id=1, topic_id=1)
            await session.commit()

            for _ in range(3):
                q = await get_random_question(session, quiz_session)
                if q:
                    await set_current_question(session, quiz_session, q)
                    await session.commit()

            # Завершаем сессию
            await end_quiz_session(session, quiz_session)
            await session.commit()

            # Вопросы должны остаться в QuizQuestion
            result = await session.execute(select(QuizQuestion))
            remaining = result.scalars().all()
            assert len(remaining) == 3, (
                f"Ожидалось 3 вопроса после завершения сессии, получено {len(remaining)}"
            )

        await engine.dispose()

    asyncio.run(_run())


def test_questions_available_after_multiple_sessions() -> None:
    """После нескольких сессий вопросы остаются в БД."""

    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add_all([
                QuizQuestion(question=f"Q{i}", answer=f"A{i}")
                for i in range(1, 6)
            ])
            await session.commit()

            # Первая сессия — использует 2 вопроса
            qs1 = await start_quiz_session(session, chat_id=1, topic_id=1)
            await session.commit()
            for _ in range(2):
                q = await get_random_question(session, qs1)
                if q:
                    await set_current_question(session, qs1, q)
                    await session.commit()
            await end_quiz_session(session, qs1)
            await session.commit()

            # Все 5 вопросов должны быть в БД
            result = await session.execute(select(QuizQuestion))
            count = len(result.scalars().all())
            assert count == 5, f"Ожидалось 5 вопросов, получено {count}"

        await engine.dispose()

    asyncio.run(_run())


def test_used_questions_marked_after_session() -> None:
    """После сессии использованные вопросы помечены в QuizUsedQuestion."""

    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add_all([
                QuizQuestion(question=f"Q{i}", answer=f"A{i}")
                for i in range(1, 4)
            ])
            await session.commit()

            qs = await start_quiz_session(session, chat_id=1, topic_id=1)
            await session.commit()

            q = await get_random_question(session, qs)
            assert q is not None
            await set_current_question(session, qs, q)
            await session.commit()

            await end_quiz_session(session, qs)
            await session.commit()

            # QuizUsedQuestion должен содержать использованный вопрос
            used_result = await session.execute(select(QuizUsedQuestion))
            used = used_result.scalars().all()
            assert len(used) >= 1, "Ожидался хотя бы один использованный вопрос"

            # Оригинальный вопрос всё ещё в БД
            orig_result = await session.execute(select(QuizQuestion))
            originals = orig_result.scalars().all()
            assert len(originals) == 3

        await engine.dispose()

    asyncio.run(_run())
