"""Почему: гарантируем глобальное исключение уже использованных вопросов викторины."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import QuizQuestion, QuizSession
from app.services.quiz import can_start_quiz, get_random_question, set_current_question


def test_get_random_question_ignores_globally_used() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            q1 = QuizQuestion(question="Вопрос 1", answer="Ответ 1")
            q2 = QuizQuestion(question="Вопрос 2", answer="Ответ 2")
            quiz_session = QuizSession(
                chat_id=1,
                topic_id=1,
                is_active=True,
                question_number=0,
            )
            session.add_all([q1, q2, quiz_session])
            await session.commit()
            await session.refresh(quiz_session)

            first = await get_random_question(session, quiz_session)
            assert first is not None
            await set_current_question(session, quiz_session, first)
            await session.commit()

            second = await get_random_question(session, quiz_session)
            assert second is not None
            assert second.question != first.question

            await set_current_question(session, quiz_session, second)
            await session.commit()

            third = await get_random_question(session, quiz_session)
            assert third is None

        await engine.dispose()

    asyncio.run(_run())


def test_can_start_quiz_checks_available_questions_only() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add_all(
                [
                    QuizQuestion(question=f"Вопрос {index}", answer=f"Ответ {index}")
                    for index in range(1, 11)
                ]
            )
            await session.commit()

            quiz_session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=0)
            session.add(quiz_session)
            await session.commit()
            await session.refresh(quiz_session)

            for _ in range(10):
                question = await get_random_question(session, quiz_session)
                assert question is not None
                await set_current_question(session, quiz_session, question)
                await session.commit()

            quiz_session.is_active = False
            await session.commit()

            can_start, reason = await can_start_quiz(session, 1, 1)
            assert can_start is False
            assert "Недостаточно новых вопросов" in reason

        await engine.dispose()

    asyncio.run(_run())
