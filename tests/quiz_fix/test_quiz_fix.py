"""Почему: защищаем вопросы викторины от удаления при завершении сессии."""

from __future__ import annotations

import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import QuizQuestion, QuizUsedQuestion
from app.services.quiz import end_quiz_session, get_random_question, set_current_question, start_quiz_session


def test_end_quiz_session_does_not_delete_questions() -> None:

    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add_all([QuizQuestion(question=f"Вопрос {i}", answer=f"Ответ {i}") for i in range(1, 4)])
            await session.commit()

            quiz_session = await start_quiz_session(session, chat_id=1, topic_id=1)
            await session.commit()

            for _ in range(3):
                q = await get_random_question(session, quiz_session)
                assert q is not None
                await set_current_question(session, quiz_session, q)
                await session.commit()

            await end_quiz_session(session, quiz_session)
            await session.commit()

            remaining_questions = (await session.execute(select(QuizQuestion))).scalars().all()
            assert len(remaining_questions) == 3

            used_rows = (await session.execute(select(QuizUsedQuestion))).scalars().all()
            assert len(used_rows) >= 1

        await engine.dispose()

    asyncio.run(_run())
