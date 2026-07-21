"""Тесты конвертера XLSX (ответ+пояснение+зачёт) и закрытия викторины при
исчерпании базы (вопросы никогда не повторяются)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, MigrationFlag, QuizQuestion
from scripts.import_quiz_xlsx import split_answer


# --- split_answer: форматы из файла владельца ---


def test_split_short_answer_and_comment() -> None:
    short, comment = split_answer("Кинотеатры. 5 центов стоил билет, а Одеон – театр.")
    assert short == "Кинотеатры"
    assert "5 центов" in comment


def test_split_zachet_becomes_alternative() -> None:
    short, comment = split_answer(
        "Собаки-поводыри (зачёт: рыбы-поводыри) У акул слабое зрение."
    )
    # «зачёт: …» уходит в альтернативы через « / »
    assert short.startswith("Собаки-поводыри")
    assert "рыбы-поводыри" in short
    assert " / " in short


def test_split_short_paren_is_alternative() -> None:
    short, comment = split_answer("Да (Yes). На GPS-трекере отобразился маршрут.")
    assert short == "Да / Yes"
    assert "GPS" in comment


def test_split_long_paren_goes_to_comment() -> None:
    short, comment = split_answer("Табун (обыгрывается сходство слов «табу» и «табун»).")
    assert short == "Табун"
    assert "сходство" in comment


def test_split_answer_only() -> None:
    short, comment = split_answer("По Великому шёлковому пути.")
    assert short == "По Великому шёлковому пути"
    assert comment == ""


def test_real_base_matches_itself() -> None:
    """Вся сконвертированная база: ответ засчитывается матчем сам себе."""
    import json
    from pathlib import Path

    from app.services.quiz import _ALT_SPLIT, check_answer

    data = json.loads(Path("data/quiz_questions.json").read_text(encoding="utf-8"))
    assert len(data) >= 1500  # база владельца — надолго
    broken = []
    for item in data[:200]:  # выборка (полная проверка — в test_quiz_questions_valid)
        for v in _ALT_SPLIT.split(item["answer"]):
            if v.strip() and not check_answer(item["answer"], v.strip()):
                broken.append(item["answer"])
    assert not broken, broken[:5]


# --- Закрытие викторины при исчерпании ---


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
    yield factory
    asyncio.run(engine.dispose())


def test_quiz_closes_once_when_bank_exhausted(db, monkeypatch) -> None:
    """База кончилась: одно уведомление владельцу+жителям, флаг, дальше тишина."""
    from app.handlers import quiz as h

    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    monkeypatch.setattr(h.settings, "admin_log_chat_id", -500)

    bot = AsyncMock()
    asyncio.run(h.start_quiz_auto(bot))  # база пуста (0 вопросов)

    # Уведомления: одно в админ-чат (-500), одно в тему игр (100).
    chats = [c.args[0] for c in bot.send_message.await_args_list]
    assert -500 in chats and 100 in chats

    async def _flag():
        async with db() as session:
            return await session.get(MigrationFlag, h._EXHAUSTED_FLAG)

    assert asyncio.run(_flag()) is not None

    # Повторный день — молчание (ни нового алерта, ни анонса).
    bot2 = AsyncMock()
    asyncio.run(h.start_quiz_auto(bot2))
    asyncio.run(h.announce_quiz_soon(bot2))
    assert bot2.send_message.await_count == 0


def test_announce_silent_when_bank_low(db, monkeypatch) -> None:
    """Анонс 19:55 молчит, если вопросов меньше, чем на тур (нет «анонс был — игры нет»)."""
    from app.handlers import quiz as h

    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)

    async def _seed_few():
        async with db() as session:
            for i in range(3):  # меньше QUESTIONS_PER_ROUND
                session.add(QuizQuestion(question=f"Q{i}?", answer=f"a{i}"))
            await session.commit()

    asyncio.run(_seed_few())
    bot = AsyncMock()
    asyncio.run(h.announce_quiz_soon(bot))
    assert bot.send_message.await_count == 0


def test_seed_reopens_quiz_after_refill(db, monkeypatch) -> None:
    """Пополнение базы снимает флаг «исчерпано» — викторина снова открыта."""
    import scripts.seed_quiz as sq

    async def _run():
        async with db() as session:
            session.add(MigrationFlag(key="quiz_bank_exhausted"))
            await session.commit()
            monkeypatch.setattr(sq, "_load_seed", lambda: [
                {"question": "Новый вопрос?", "answer": "ответ", "comment": "пояснение"},
            ])
            await sq.seed_quiz_questions(session)
            await session.commit()
            flag = await session.get(MigrationFlag, "quiz_bank_exhausted")
            from sqlalchemy import select
            row = (await session.execute(select(QuizQuestion))).scalars().one()
            return flag, row

    flag, row = asyncio.run(_run())
    assert flag is None  # флаг снят
    assert row.comment == "пояснение"  # пояснение доехало до БД


def test_reveal_includes_comment() -> None:
    """Пояснение показывается при развязке вопроса."""
    from app.handlers.quiz import _reveal_text
    from app.services.quiz import QuizState

    state = QuizState(
        phase="asking", question_ids=[1], current_answer="Кинотеатры",
        current_comment="5 центов стоил билет, а Одеон – театр.",
    )
    text = _reveal_text(state, "Вася")
    assert "Кинотеатры" in text
    assert "💬 5 центов" in text
