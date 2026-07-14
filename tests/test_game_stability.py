"""Регрессии вечера запуска «21»: зависшие столы, поздние колбэки, токены гейта."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, GameState, UserStat


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
    monkeypatch.setattr("app.handlers.blackjack.get_session", _get_session)
    yield factory
    asyncio.run(engine.dispose())


def _game_message(user_id: int = 7) -> SimpleNamespace:
    reply_msg = SimpleNamespace(message_id=555, chat=SimpleNamespace(id=100))
    return SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_thread_id=42,
        message_id=900,
        from_user=SimpleNamespace(id=user_id, username="vasya", full_name="Вася"),
        reply=AsyncMock(return_value=reply_msg),
    )


def test_21_reopens_stuck_betting_table(db, monkeypatch) -> None:
    """Регресс «зависла моя игра»: /21 при висящем столе без ставки не отвечает
    «доиграй её», а молча открывает новый стол."""
    from app.handlers import blackjack as h
    from app.services import blackjack as bj

    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    monkeypatch.setattr(h, "is_game_time_allowed", lambda a, b: True)

    async def _prepare():
        async with db() as session:
            await bj.save_game(session, 7, 100, bj.new_betting_state(message_id=111))
            await session.commit()

    asyncio.run(_prepare())

    message = _game_message(user_id=7)
    bot = AsyncMock()
    asyncio.run(h.cmd_blackjack(message, bot))

    # Стол переоткрыт: новое сообщение со ставками отправлено, партия свежая.
    assert message.reply.await_count == 1
    text = message.reply.await_args.args[0]
    assert "Стол готов" in text

    async def _check():
        async with db() as session:
            row = (await session.execute(select(GameState))).scalars().one()
            state = bj.BlackjackState.from_json(row.state_json)
            return state

    state = asyncio.run(_check())
    assert state.phase == "betting"
    assert state.message_id == 555  # привязан к новому сообщению


def test_21_resends_table_for_alive_game(db, monkeypatch) -> None:
    """Живая партия не теряется: /21 пересылает стол с кнопками (анти-завис UI)."""
    from app.handlers import blackjack as h
    from app.services import blackjack as bj

    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    monkeypatch.setattr(h, "is_game_time_allowed", lambda a, b: True)

    async def _prepare():
        async with db() as session:
            await bj.save_game(session, 7, 100, bj.new_betting_state(message_id=111))
            state, reason = await bj.place_bet_and_deal(session, 7, 100, 25, "Вася")
            assert reason is None
            await session.commit()

    asyncio.run(_prepare())

    message = _game_message(user_id=7)
    bot = AsyncMock()
    asyncio.run(h.cmd_blackjack(message, bot))

    assert message.reply.await_count == 1
    text = message.reply.await_args.args[0]
    assert "ставка 25" in text  # это текущий стол, а не новый

    async def _check():
        async with db() as session:
            row = (await session.execute(select(GameState))).scalars().one()
            return bj.BlackjackState.from_json(row.state_json)

    state = asyncio.run(_check())
    assert state.phase == "playing"  # партия и ставка не тронуты
    assert state.bet == 25
    assert state.message_id == 555  # кнопки живут на новом сообщении


def test_safe_answer_swallows_stale_callback() -> None:
    """«query is too old» от позднего answer не роняет обработчик кнопки."""
    from aiogram.exceptions import TelegramBadRequest
    from app.handlers.blackjack import _safe_answer

    callback = SimpleNamespace(
        answer=AsyncMock(side_effect=TelegramBadRequest(
            method=None, message="query is too old and response timeout expired"
        ))
    )
    asyncio.run(_safe_answer(callback, "тост"))  # не должно бросить
    assert callback.answer.await_count == 1


def test_gate_intent_ai_called_only_after_prefilter(monkeypatch) -> None:
    """Экономия токенов: detect_gate_intent не дёргается на болтовню в топике
    шлагбаума — только после локального префильтра явных слов заявки."""
    from app.handlers import moderation

    monkeypatch.setattr(moderation.settings, "forum_chat_id", 100)
    monkeypatch.setattr(moderation.settings, "topic_gate", 55)
    monkeypatch.setattr(moderation.settings, "ai_enabled", True)
    monkeypatch.setattr(moderation.settings, "ai_feature_moderation", False)
    monkeypatch.setattr(moderation, "is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(moderation, "contains_forbidden_link", lambda _: False)
    monkeypatch.setattr(moderation, "_get_topic_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(moderation, "_store_message_log", AsyncMock())
    monkeypatch.setattr(moderation, "_check_flood", AsyncMock(return_value=False))
    monkeypatch.setattr(moderation, "is_training_mode", lambda: False)

    detect_mock = AsyncMock()
    monkeypatch.setattr("app.services.ai_tasks.detect_gate_intent", detect_mock)

    def _msg(text: str, mid: int) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=100),
            from_user=SimpleNamespace(id=777, mention_html=lambda: "@u"),
            text=text,
            message_id=mid,
            message_thread_id=55,
            delete=AsyncMock(),
        )

    # Болтовня без слов заявки — AI не вызывается.
    moderation._MODERATED_MSG_IDS.clear()
    asyncio.run(moderation.run_moderation(
        _msg("да уж, опять очередь на выезде собралась утром", 1), AsyncMock()
    ))
    assert detect_mock.await_count == 0
