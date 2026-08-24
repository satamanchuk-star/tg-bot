"""Аналитика: отчёт собирается, склонения верные, битый блок не роняет отчёт."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as m
from app.db import Base


def _engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:")


async def _prepared(seed: bool):
    engine = _engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if seed:
        now = datetime.now(timezone.utc)
        async with factory() as session:
            for i in range(20):
                session.add(m.MessageLog(chat_id=100, topic_id=42, user_id=1000 + i % 4, text="x"))
            session.add(m.AiTaskLog(
                date_key=now.date().isoformat(), task="reply", model="claude-haiku-4-5",
                tokens_used=900, cost_usd=0.01,
            ))
            session.add(m.AiFeedback(chat_id=100, user_id=1, bot_message_id=1,
                                     prompt_text="q", reply_text="a", rating=-1))
            session.add(m.QuizRound(user_id=1, chat_id=100, correct_answers=5,
                                    is_winner=True, coins_awarded=175, display_name="U1"))
            for i in range(30):
                session.add(m.QuizQuestion(question=f"q{i}", answer=f"a{i}",
                                           used_at=now if i < 15 else None))
            session.add(m.QuizAnswerMiss(chat_id=100, question_id=1,
                                         correct_answer="Безопасная бритва",
                                         given_text="лезвие", verdict="near"))
            session.add(m.GameRound(user_id=1, chat_id=100, bet=25, result="win", payout=50,
                                    player_hand="К♥ 7♠", dealer_hand="9♦ 8♣"))
            session.add(m.UserStat(user_id=1, chat_id=100, coins=250))
            session.add(m.Place(name="Аптека", category="medical", address="ул. 1",
                                is_active=True, verified_at=now - timedelta(days=200)))
            await session.commit()
    return engine, factory


def _report(seed: bool = True, days: int = 7) -> str:
    from app.services.analytics import build_analytics_report

    async def run():
        engine, factory = await _prepared(seed)
        async with factory() as session:
            text = await build_analytics_report(session, days=days)
        await engine.dispose()
        return text

    return asyncio.run(run())


def test_report_has_all_sections() -> None:
    text = _report()
    for marker in ("Жители", "Ассистент", "Викторина", "«21»", "В обороте", "База"):
        assert marker in text, f"нет блока {marker}:\n{text}"


def test_report_shows_matcher_misses_with_examples() -> None:
    """Главная ценность отчёта: видно, какие ответы бот не засчитал."""
    text = _report()
    assert "Не засчитано" in text
    assert "Безопасная бритва" in text and "лезвие" in text


def test_report_shows_bank_depth() -> None:
    """Владелец должен видеть, на сколько вечеров хватит вопросов."""
    text = _report()
    assert "Банк: 15 вопросов" in text and "вечер" in text


def test_report_on_empty_database_does_not_crash() -> None:
    text = _report(seed=False)
    assert "Аналитика" in text
    assert "тишина в чате" in text
    assert "туров не было" in text


def test_broken_block_does_not_kill_report(monkeypatch) -> None:
    """Один упавший блок не должен лишать владельца всего отчёта."""
    from app.services import analytics

    async def _boom(*args, **kwargs):
        raise RuntimeError("база подвела")

    monkeypatch.setattr(analytics, "_quiz_block", _boom)
    text = _report()
    assert "не собрался" in text      # честно сообщили о проблеме
    assert "Жители" in text           # остальные блоки на месте


@pytest.mark.parametrize(("n", "expected"), [
    (1, "вопрос"), (2, "вопроса"), (5, "вопросов"),
    (11, "вопросов"), (21, "вопрос"), (104, "вопроса"),
])
def test_russian_plurals(n: int, expected: str) -> None:
    from app.services.analytics import _plural

    assert _plural(n, "вопрос", "вопроса", "вопросов") == expected


def test_daily_report_uses_same_builder(monkeypatch) -> None:
    """Вечерняя сводка 22:30 — тот же отчёт за сутки, а не отдельная реализация."""
    import inspect

    from app.services import daily_report

    source = inspect.getsource(daily_report.send_daily_report)
    assert "build_analytics_report" in source
    assert "days=1" in source
