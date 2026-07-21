"""Тесты driver'а викторины: полный тур без дедлока, финиш, гонки события.

Регрессии на аудит: реентрантный дедлок (_advance→_finish под одним локом) и
потерянное пробуждение (event.clear после ответа).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, QuizQuestion, QuizRound, QuizSession, UserStat


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/quiz.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _get_session():
        async with factory() as session:
            yield session

    asyncio.run(_prepare())
    monkeypatch.setattr("app.handlers.quiz.get_session", _get_session)
    monkeypatch.setattr("app.services.quiz.get_session", _get_session, raising=False)
    yield factory
    asyncio.run(engine.dispose())


def _make_bot() -> AsyncMock:
    """Бот-мок с настоящим message_id в ответе send_message (для state_json)."""
    bot = AsyncMock()
    counter = {"n": 0}

    async def _send(*args, **kwargs):
        counter["n"] += 1
        return SimpleNamespace(message_id=5000 + counter["n"], chat=SimpleNamespace(id=100))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


def _prime(monkeypatch, questions_per_round=2, seconds=1, brk=0):
    from app.handlers import quiz as h
    from app.services import quiz as q
    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    monkeypatch.setattr(q, "QUESTIONS_PER_ROUND", questions_per_round)
    monkeypatch.setattr(q, "SECONDS_PER_QUESTION", seconds)
    monkeypatch.setattr(q, "BREAK_SECONDS", brk)
    monkeypatch.setattr(h.q, "QUESTIONS_PER_ROUND", questions_per_round)
    monkeypatch.setattr(h.q, "SECONDS_PER_QUESTION", seconds)
    monkeypatch.setattr(h.q, "BREAK_SECONDS", brk)
    h._chat_locks.clear()
    h._answer_events.clear()
    h._running.clear()


def test_full_round_completes_without_deadlock(db, monkeypatch) -> None:
    """Полный тур по таймауту доходит до финиша (регресс дедлока)."""
    from app.handlers import quiz as h

    _prime(monkeypatch, questions_per_round=2, seconds=1, brk=0)

    async def _seed():
        async with db() as session:
            session.add(QuizQuestion(question="Столица Франции?", answer="Париж"))
            session.add(QuizQuestion(question="2+2?", answer="4"))
            await session.commit()

    asyncio.run(_seed())
    bot = _make_bot()

    async def _run():
        reason = await h._launch_quiz(bot, 100)
        assert reason is None
        # Ждём завершения driver-таска (2 вопроса × 1 сек + пауза) с запасом.
        await asyncio.wait_for(h._running[100], timeout=10)

    asyncio.run(_run())

    async def _check():
        async with db() as session:
            sess = (await session.execute(select(QuizSession))).scalars().all()
            return sess

    # Сессия удалена = тур честно завершён (не завис).
    assert asyncio.run(_check()) == []


def test_missing_last_question_still_finishes(db, monkeypatch) -> None:
    """Регресс дедлока: если последний вопрос пропал из БД, тур всё равно
    завершается, а не виснет под захваченным локом."""
    from app.handlers import quiz as h
    from app.services import quiz as q

    _prime(monkeypatch, questions_per_round=2, seconds=1, brk=0)

    async def _seed_and_break():
        async with db() as session:
            session.add(QuizQuestion(id=1, question="Q1?", answer="a1"))
            session.add(QuizQuestion(id=2, question="Q2?", answer="a2"))
            await session.commit()
        # Стартуем сессию вручную с несуществующим 2-м вопросом (id=999).
        async with db() as session:
            state = q.QuizState(
                phase="break", question_ids=[1, 999], index=0,
                current_answer="a1", question_text="Q1?",
            )
            await q.save_session(session, 100, 42, state)
            await session.commit()

    asyncio.run(_seed_and_break())
    bot = _make_bot()

    async def _run():
        h._start_driver(bot, 100)
        await asyncio.wait_for(h._running[100], timeout=10)

    asyncio.run(_run())

    async def _check():
        async with db() as session:
            return (await session.execute(select(QuizSession))).scalars().all()

    assert asyncio.run(_check()) == []  # завершился, не завис


def test_correct_answer_ends_question_fast(db, monkeypatch) -> None:
    """Верный ответ закрывает вопрос сразу, а не досиживает таймер (регресс
    потерянного пробуждения). Таймер 30с, но тур завершится за доли секунды."""
    from app.handlers import quiz as h

    _prime(monkeypatch, questions_per_round=1, seconds=30, brk=0)

    async def _seed():
        async with db() as session:
            session.add(QuizQuestion(question="Столица Франции?", answer="Париж"))
            await session.commit()

    asyncio.run(_seed())
    bot = _make_bot()

    async def _run():
        await h._launch_quiz(bot, 100)
        await asyncio.sleep(0.05)  # дать driver дойти до ожидания события
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=100), message_thread_id=42,
            text="Париж",
            from_user=SimpleNamespace(id=7, username="u", full_name="U"),
        )
        await h.on_answer(msg, bot)
        # Если бы пробуждение терялось — тур висел бы 30с. Ждём максимум 5.
        await asyncio.wait_for(h._running[100], timeout=5)

    asyncio.run(_run())

    async def _check():
        async with db() as session:
            u = await session.get(UserStat, {"user_id": 7, "chat_id": 100})
            rounds = (await session.execute(select(QuizRound))).scalars().all()
            return u, rounds

    u, rounds = asyncio.run(_check())
    # Игрок получил +15 за ответ и +100 бонус победителя.
    from app.services.quiz import COINS_PER_CORRECT, WINNER_BONUS
    assert u.coins == 200 + COINS_PER_CORRECT + WINNER_BONUS
    assert len(rounds) == 1 and rounds[0].is_winner is True
