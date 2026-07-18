"""Тесты импорта вопросов: парсинг ответа ИИ и дедуп-вставка в пул."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, QuizQuestion
from app.services.quiz_import import _parse_pairs, insert_new_questions


@pytest.fixture()
def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())
    yield factory
    asyncio.run(engine.dispose())


def test_parse_clean_json_array() -> None:
    raw = '[{"question": "Столица Франции?", "answer": "Париж", "category": "гео"}]'
    pairs = _parse_pairs(raw)
    assert len(pairs) == 1
    assert pairs[0]["question"] == "Столица Франции?"
    assert pairs[0]["answer"] == "Париж"
    assert pairs[0]["category"] == "гео"


def test_parse_strips_markdown_fence() -> None:
    raw = '```json\n[{"question": "2+2?", "answer": "4"}]\n```'
    pairs = _parse_pairs(raw)
    assert len(pairs) == 1 and pairs[0]["answer"] == "4"


def test_parse_extracts_array_from_wrapper_text() -> None:
    raw = 'Вот вопросы: [{"question": "Q?", "answer": "A"}] — готово.'
    pairs = _parse_pairs(raw)
    assert len(pairs) == 1


def test_parse_drops_long_answers_and_empties() -> None:
    raw = (
        '[{"question": " Q1?", "answer": "короткий"},'
        '{"question": "q2?", "answer": "' + "очень длинный ответ " * 5 + '"},'
        '{"question": "", "answer": "нет вопроса"},'
        '{"question": "q4?", "answer": ""}]'
    )
    pairs = _parse_pairs(raw)
    assert len(pairs) == 1  # только первый валиден
    assert pairs[0]["question"] == "Q1?"  # регистр вопроса сохраняется


def test_parse_garbage_returns_empty() -> None:
    assert _parse_pairs("это не json совсем") == []
    assert _parse_pairs("") == []


def test_insert_dedups_against_existing(db) -> None:
    async def _run():
        async with db() as session:
            session.add(QuizQuestion(question="Столица Франции?", answer="Париж"))
            await session.commit()
            pairs = [
                {"question": "Столица Франции?", "answer": "париж"},  # дубль (регистр)
                {"question": "Столица Италии?", "answer": "Рим"},    # новый
            ]
            added = await insert_new_questions(session, pairs)
            await session.commit()
            total = len((await session.execute(select(QuizQuestion))).scalars().all())
            return added, total

    added, total = asyncio.run(_run())
    assert added == 1  # только Рим
    assert total == 2


def test_insert_dedups_within_batch(db) -> None:
    async def _run():
        async with db() as session:
            pairs = [
                {"question": "Q?", "answer": "A"},
                {"question": "q?", "answer": "a"},  # тот же по нормализации
            ]
            added = await insert_new_questions(session, pairs)
            await session.commit()
            return added

    assert asyncio.run(_run()) == 1
