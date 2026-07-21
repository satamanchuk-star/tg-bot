"""Симуляция полного тура с точной бухгалтерией — аудит «подсчёт сделали неверно».

Проверяется всё движение очков и монет: кто сколько верных забрал, монеты за
ответы, бонус победителя, история QuizRound, итоговая таблица, нумерация
вопросов при снятом из базы вопросе.
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
    yield factory
    asyncio.run(engine.dispose())


def _make_bot() -> AsyncMock:
    """Бот-мок: send_message возвращает объект с настоящим message_id (иначе
    board_message_id в state_json — AsyncMock, и JSON-сериализация падает)."""
    bot = AsyncMock()
    counter = {"n": 0}

    async def _send(*args, **kwargs):
        counter["n"] += 1
        return SimpleNamespace(message_id=5000 + counter["n"], chat=SimpleNamespace(id=100))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


def _msg(text: str, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_thread_id=42,
        message_id=1000 + user_id,
        text=text,
        from_user=SimpleNamespace(id=user_id, username=f"u{user_id}", full_name=f"U{user_id}"),
    )


def _prime(monkeypatch, seconds=1, brk=0):
    from app.handlers import quiz as h
    from app.services import quiz as q
    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    monkeypatch.setattr(q, "QUESTIONS_PER_ROUND", 3)
    monkeypatch.setattr(q, "SECONDS_PER_QUESTION", seconds)
    monkeypatch.setattr(q, "BREAK_SECONDS", brk)
    monkeypatch.setattr(h.q, "QUESTIONS_PER_ROUND", 3)
    monkeypatch.setattr(h.q, "SECONDS_PER_QUESTION", seconds)
    monkeypatch.setattr(h.q, "BREAK_SECONDS", brk)
    h._chat_locks.clear()
    h._answer_events.clear()
    h._running.clear()


def test_full_round_exact_accounting(db, monkeypatch) -> None:
    """Тур из 3 вопросов, 2 игрока: точная сверка очков, монет, истории.

    Сценарий: В1 — Аня ошибается, потом отвечает верно; В2 — Петя верно
    (Аня после него — не считается); В3 — никто. Итог: Аня 1, Петя 1 —
    оба победители (+100 каждому).
    """
    from app.handlers import quiz as h
    from app.services import quiz as q
    from app.services.quiz import COINS_PER_CORRECT, WINNER_BONUS

    _prime(monkeypatch, seconds=2, brk=0)

    async def _run():
        async with db() as session:
            for i, (qq, aa) in enumerate([
                ("Столица Франции?", "Париж"),
                ("Царь зверей?", "лев"),
                ("2+2?", "4"),
            ]):
                session.add(QuizQuestion(id=i + 1, question=qq, answer=aa))
            await session.commit()

        bot = _make_bot()
        reason = await h._launch_quiz(bot, 100)
        assert reason is None
        await asyncio.sleep(0.05)

        async def _current_answer() -> str:
            async for session in h.get_session():
                state = await h.q.load_session(session, 100)
                await session.commit()
                return state.current_answer
            return ""

        # Вопрос 1 (порядок случайный — отвечаем по фактическому эталону):
        # Аня ошибается, затем верно; Петя после — уже поздно.
        answer1 = await _current_answer()
        await h.on_answer(_msg("явно неверный ответ", 1), bot)   # мимо
        await h.on_answer(_msg(f"это {answer1}", 1), bot)        # верно, Аня забирает
        await h.on_answer(_msg(answer1, 2), bot)                 # поздно — забран
        await _wait_phase(h, "asking", index=1)

        # Вопрос 2: Петя отвечает верно.
        answer2 = await _current_answer()
        await h.on_answer(_msg(f"{answer2} конечно", 2), bot)
        await _wait_phase(h, "asking", index=2)

        # Вопрос 3: никто не отвечает — таймаут (2 сек).
        await asyncio.wait_for(h._running[100], timeout=15)

        async with db() as session:
            u1 = await session.get(UserStat, {"user_id": 1, "chat_id": 100})
            u2 = await session.get(UserStat, {"user_id": 2, "chat_id": 100})
            rounds = (await session.execute(select(QuizRound))).scalars().all()
            sessions = (await session.execute(select(QuizSession))).scalars().all()
            return u1, u2, rounds, sessions

    async def _wait_phase(h, phase: str, index: int) -> None:
        """Ждём, пока driver продвинет тур до нужного вопроса."""
        for _ in range(100):
            await asyncio.sleep(0.05)
            async for session in __import__("app.handlers.quiz", fromlist=["get_session"]).get_session():
                state = await h.q.load_session(session, 100)
                await session.commit()
                break
            if state is not None and state.phase == phase and state.index == index:
                return
        raise AssertionError(f"тур не дошёл до {phase}/{index}")

    u1, u2, rounds, sessions = asyncio.run(_run())

    # Монеты: ровно 1 верный у каждого + бонус победителя обоим (ничья 1:1).
    assert u1.coins == 200 + COINS_PER_CORRECT + WINNER_BONUS
    assert u2.coins == 200 + COINS_PER_CORRECT + WINNER_BONUS

    # История: по строке на участника, оба победители, суммы сходятся.
    assert len(rounds) == 2
    for r in rounds:
        assert r.correct_answers == 1
        assert r.is_winner is True
        assert r.coins_awarded == COINS_PER_CORRECT + WINNER_BONUS

    assert sessions == []  # сессия закрыта


def test_removed_question_keeps_numbering(db, monkeypatch) -> None:
    """Вопрос снят из базы во время тура: нумерация сплошная, слот не съеден."""
    from app.handlers import quiz as h
    from app.services import quiz as q

    _prime(monkeypatch, seconds=1, brk=0)

    async def _run():
        async with db() as session:
            session.add(QuizQuestion(id=1, question="Q1?", answer="a1"))
            session.add(QuizQuestion(id=3, question="Q3?", answer="a3"))
            await session.commit()
        # Сессия вручную: второй вопрос (id=2) не существует.
        async with db() as session:
            state = q.QuizState(
                phase="break", question_ids=[1, 2, 3], index=0,
                current_answer="a1", question_text="Q1?",
            )
            await q.save_session(session, 100, 42, state)
            await session.commit()

        bot = _make_bot()
        h._start_driver(bot, 100)
        await asyncio.wait_for(h._running[100], timeout=15)
        return bot

    bot = asyncio.run(_run())

    # Заголовки отправленных вопросов: сплошная нумерация из фактического
    # количества («1/2», «2/2») — без скачка «3/3».
    texts = [c.args[1] for c in bot.send_message.await_args_list if "Вопрос" in str(c.args[1])]
    assert any("Вопрос 2/2" in t for t in texts), texts
    assert not any("/3" in t for t in texts), texts