"""Тесты петли роста: лог «не знаю»-вопросов, дайджест, ответ админа."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, RagMessage, UnansweredQuestion


@pytest.fixture()
def db_session_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())

    async def _get_session():
        async with factory() as session:
            yield session

    # Подменяем get_session во всех местах, где его использует петля роста
    monkeypatch.setattr("app.services.unanswered.get_session", _get_session)
    yield factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())


def test_log_unanswered_dedupes_by_norm_key(db_session_factory) -> None:
    from app.services.unanswered import log_unanswered

    asyncio.run(log_unanswered(1, "Где взять ключ от домофона?"))
    asyncio.run(log_unanswered(1, "где взять ключ от домофона"))  # повтор
    asyncio.run(log_unanswered(1, "Сколько стоит парковка?"))

    async def _check():
        async with db_session_factory() as session:
            rows = (await session.execute(select(UnansweredQuestion))).scalars().all()
            return {r.norm_key: r.hits for r in rows}

    result = asyncio.run(_check())
    assert len(result) == 2
    assert 2 in result.values()  # повторный вопрос сгруппирован


def test_save_admin_answer_writes_rag_and_closes(db_session_factory) -> None:
    from app.services.unanswered import log_unanswered, save_admin_answer

    asyncio.run(log_unanswered(1, "Где взять ключ от домофона?"))

    async def _get_id():
        async with db_session_factory() as session:
            return (await session.scalar(select(UnansweredQuestion.id)))

    qid = asyncio.run(_get_id())
    question = asyncio.run(save_admin_answer(qid, "В офисе УК, 500 рублей.", admin_id=42))
    assert question is not None and "домофона" in question

    async def _check():
        async with db_session_factory() as session:
            q = await session.get(UnansweredQuestion, qid)
            rag = (await session.execute(select(RagMessage))).scalars().all()
            return q.status, rag

    status, rag = asyncio.run(_check())
    assert status == "answered"
    assert len(rag) == 1
    assert rag[0].is_admin is True
    assert "500 рублей" in rag[0].message_text


def test_digest_sends_top_questions(db_session_factory) -> None:
    from app.services.unanswered import log_unanswered, send_unanswered_digest

    asyncio.run(log_unanswered(1, "Где взять ключ от домофона?"))
    asyncio.run(log_unanswered(1, "Сколько стоит парковка на месяц?"))

    bot = AsyncMock()
    asyncio.run(send_unanswered_digest(bot))
    # Заголовок + 2 вопроса
    assert bot.send_message.await_count == 3


def test_digest_silent_when_empty(db_session_factory) -> None:
    from app.services.unanswered import send_unanswered_digest

    bot = AsyncMock()
    asyncio.run(send_unanswered_digest(bot))
    assert bot.send_message.await_count == 0


# Реестр «сообщение дайджеста → вопрос» стал персистентным (аудит-4):
# сценарий register → peek → закрытие ответом покрыт в
# tests/test_audit4_fixes.py::test_pending_answer_survives_restart.
