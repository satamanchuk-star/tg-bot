"""Почему: фиксируем lock-поведение викторины через запрет второй активной сессии."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import QuizQuestion
from app.services.quiz import QUIZ_QUESTIONS_COUNT, can_start_quiz, start_quiz_session


def test_cannot_start_second_active_session_same_chat_topic() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add_all([
                QuizQuestion(question=f"Q{i}", answer=f"A{i}")
                for i in range(1, QUIZ_QUESTIONS_COUNT + 2)
            ])
            await session.commit()

            await start_quiz_session(session, chat_id=1, topic_id=10)
            await session.commit()

            allowed, reason = await can_start_quiz(session, chat_id=1, topic_id=10)
            assert allowed is False
            assert "уже запущена" in reason

        await engine.dispose()

    asyncio.run(_run())


def test_sessions_independent_for_different_topics() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            session.add_all([
                QuizQuestion(question=f"Q{i}", answer=f"A{i}")
                for i in range(1, QUIZ_QUESTIONS_COUNT + 2)
            ])
            await session.commit()

            await start_quiz_session(session, chat_id=1, topic_id=10)
            await session.commit()

            allowed, reason = await can_start_quiz(session, chat_id=1, topic_id=11)
            assert allowed is True
            assert reason == ""

        await engine.dispose()

    asyncio.run(_run())
