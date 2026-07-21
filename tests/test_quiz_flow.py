"""Тесты сессии викторины: first-wins, скоринг, персистентность, seed, история."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, QuizQuestion, QuizRound, QuizSession, UserStat


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


def _add_questions(session, n: int) -> None:
    for i in range(n):
        session.add(QuizQuestion(question=f"Вопрос {i}?", answer=f"ответ{i}"))


def test_state_json_roundtrip() -> None:
    from app.services.quiz import QuizState

    state = QuizState(
        phase="asking", question_ids=[1, 2, 3], index=1, current_answer="Москва",
        question_text="Столица?", winner_user_id=42,
        scores={"42": {"name": "Аня", "correct": 2}},
    )
    restored = QuizState.from_json(state.to_json())
    assert restored is not None
    assert restored.index == 1
    assert restored.winner_user_id == 42
    assert restored.scores["42"]["correct"] == 2


def test_state_rejects_bad_payload() -> None:
    from app.services.quiz import QuizState

    assert QuizState.from_json("не json") is None
    assert QuizState.from_json('{"version": 999}') is None


def test_pick_questions_never_repeats(db) -> None:
    """Вопросы не повторяются (решение владельца): recycle убран, свежие
    кончились — возвращается остаток, викторина закрывается."""
    from app.services import quiz as q

    async def _run():
        async with db() as session:
            _add_questions(session, 20)
            await session.commit()
            first = await q.pick_questions(session, 15)
            await session.commit()
            second = await q.pick_questions(session, 15)  # осталось только 5
            await session.commit()
            fresh_left = await q.count_fresh_questions(session)
            return len(first), len(second), fresh_left, {x.id for x in first} & {x.id for x in second}

    n1, n2, fresh, overlap = asyncio.run(_run())
    assert n1 == 15
    assert n2 == 5  # только остаток, БЕЗ повторного использования
    assert fresh == 0
    assert overlap == set()  # ни один вопрос не выдан дважды


def test_pick_questions_insufficient(db) -> None:
    from app.services import quiz as q

    async def _run():
        async with db() as session:
            _add_questions(session, 5)  # меньше тура
            await session.commit()
            picked = await q.pick_questions(session, 15)
            await session.commit()
            return len(picked)

    # Всего 5 вопросов — вернёт максимум 5 (запуск потом откажет).
    assert asyncio.run(_run()) == 5


def test_record_round_and_leaderboard(db) -> None:
    from app.services import quiz as q

    async def _run():
        async with db() as session:
            scores = {
                "1": {"name": "Аня", "correct": 5},
                "2": {"name": "Петя", "correct": 2},
            }
            await q.record_round(session, chat_id=10, scores=scores,
                                 winner_ids={1}, winner_bonus=100)
            await session.commit()
            rounds = (await session.execute(select(QuizRound))).scalars().all()
            board = await q.get_alltime_leaderboard(session, 10)
            return rounds, board

    rounds, board = asyncio.run(_run())
    assert len(rounds) == 2
    winner_row = next(r for r in rounds if r.user_id == 1)
    assert winner_row.is_winner is True
    assert winner_row.coins_awarded == 5 * q.COINS_PER_CORRECT + 100
    # Лидерборд: Аня первая (5 верных, 1 победа)
    assert board[0][0] == "Аня"
    assert board[0][1] == 5 and board[0][2] == 1


def test_session_persistence(db) -> None:
    from app.services import quiz as q

    async def _run():
        async with db() as session:
            state = q.QuizState(phase="asking", question_ids=[1, 2], current_answer="X")
            await q.save_session(session, 10, 42, state)
            await session.commit()
            loaded = await q.load_session(session, 10)
            return loaded

    loaded = asyncio.run(_run())
    assert loaded is not None and loaded.phase == "asking"
    assert loaded.question_ids == [1, 2]


def test_stale_detection() -> None:
    from app.services.quiz import QuizState

    now = datetime.now(timezone.utc)
    fresh = QuizState(phase="asking", question_ids=[1], updated_at=now.isoformat())
    assert fresh.is_stale(now) is False
    old = QuizState(phase="asking", question_ids=[1],
                    updated_at=(now - timedelta(minutes=11)).isoformat())
    assert old.is_stale(now) is True


def test_seed_quiz_from_file(db) -> None:
    """Сид грузит вопросы из data/quiz_questions.json (реальный файл)."""
    from scripts.seed_quiz import seed_quiz_questions

    async def _run():
        async with db() as session:
            total = await seed_quiz_questions(session)
            await session.commit()
            # Повторный вызов идемпотентен (не задваивает).
            total2 = await seed_quiz_questions(session)
            await session.commit()
            return total, total2

    total, total2 = asyncio.run(_run())
    assert total >= 100  # в базе несколько сотен вопросов
    assert total == total2  # идемпотентность
