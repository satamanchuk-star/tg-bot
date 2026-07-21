"""Тесты приёма ответов викторины: first-wins атомарен, неверный не жжёт, гейт темы."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, UserStat


@pytest.fixture()
def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _get_session():
        async with factory() as session:
            yield session

    asyncio.run(_prepare())
    monkeypatch.setattr("app.handlers.quiz.get_session", _get_session)
    yield factory
    asyncio.run(engine.dispose())


def _answer_msg(text: str, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_thread_id=42,
        text=text,
        from_user=SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U{user_id}"),
    )


def _prime_topic(monkeypatch) -> None:
    from app.handlers import quiz as h
    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    h._chat_locks.clear()
    h._answer_events.clear()
    h._running.clear()


async def _start_asking(db, answer: str) -> None:
    from app.services import quiz as q
    async with db() as session:
        state = q.QuizState(
            phase="asking", question_ids=[1, 2], index=0,
            current_answer=answer, question_text="Столица?",
        )
        state.question_started_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat()
        await q.save_session(session, 100, 42, state)
        await session.commit()


def test_first_correct_wins_and_awards(db, monkeypatch) -> None:
    from app.handlers import quiz as h
    from app.services import quiz as q

    _prime_topic(monkeypatch)
    asyncio.run(_start_asking(db, "Москва"))

    # Первый верный.
    asyncio.run(h.on_answer(_answer_msg("Москва", user_id=1), AsyncMock()))
    # Второй верный в тот же вопрос — уже поздно, вопрос забран.
    asyncio.run(h.on_answer(_answer_msg("Москва", user_id=2), AsyncMock()))

    async def _check():
        async with db() as session:
            state = await q.load_session(session, 100)
            u1 = await session.get(UserStat, {"user_id": 1, "chat_id": 100})
            u2 = await session.get(UserStat, {"user_id": 2, "chat_id": 100})
            return state, u1, u2

    state, u1, u2 = asyncio.run(_check())
    assert state.winner_user_id == 1
    assert state.scores["1"]["correct"] == 1
    assert "2" not in state.scores  # второй ничего не получил
    assert u1.coins == 200 + q.COINS_PER_CORRECT
    assert u2 is None  # второму даже баланс не создавали


def test_wrong_answer_does_not_burn_attempt(db, monkeypatch) -> None:
    """Неверный ответ не мешает потом ответить верно (фикс старого бага)."""
    from app.handlers import quiz as h
    from app.services import quiz as q

    _prime_topic(monkeypatch)
    asyncio.run(_start_asking(db, "Москва"))

    asyncio.run(h.on_answer(_answer_msg("привет", user_id=1), AsyncMock()))  # болтовня
    asyncio.run(h.on_answer(_answer_msg("Москва", user_id=1), AsyncMock()))  # теперь верно

    async def _check():
        async with db() as session:
            return await q.load_session(session, 100)

    state = asyncio.run(_check())
    assert state.winner_user_id == 1
    assert state.scores["1"]["correct"] == 1


def test_answer_ignored_outside_asking_phase(db, monkeypatch) -> None:
    from app.handlers import quiz as h
    from app.services import quiz as q

    _prime_topic(monkeypatch)

    async def _prep():
        async with db() as session:
            state = q.QuizState(phase="break", question_ids=[1], current_answer="Москва")
            await q.save_session(session, 100, 42, state)
            await session.commit()

    asyncio.run(_prep())
    asyncio.run(h.on_answer(_answer_msg("Москва", user_id=1), AsyncMock()))

    async def _check():
        async with db() as session:
            return await q.load_session(session, 100)

    assert asyncio.run(_check()).scores == {}  # в паузе ответы не считаются


def test_topic_gate_filter(monkeypatch) -> None:
    """Фильтр приёма срабатывает только в теме игр (иначе перехватит форум)."""
    from app.handlers import quiz as h
    _prime_topic(monkeypatch)

    in_topic = _answer_msg("Москва", user_id=1)
    assert h._is_games_topic_answer(in_topic) is True

    other_topic = _answer_msg("Москва", user_id=1)
    other_topic.message_thread_id = 999
    assert h._is_games_topic_answer(other_topic) is False

    command = _answer_msg("/start", user_id=1)
    assert h._is_games_topic_answer(command) is False  # команды не перехватываем
