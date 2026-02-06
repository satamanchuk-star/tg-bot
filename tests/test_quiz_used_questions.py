"""Почему: гарантируем глобальное исключение уже использованных вопросов викторины."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import QuizQuestion, QuizSession
from app.services.quiz import get_random_question, set_current_question


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
