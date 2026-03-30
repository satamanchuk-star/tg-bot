"""Тесты лотереи: покупка билетов, банк, розыгрыш."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import LotteryTicket, UserStat
from app.services.lottery import (
    TICKET_COST,
    buy_ticket,
    current_week_key,
    draw_winner,
    get_current_pot,
    get_tickets_for_week,
)


def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _setup_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _create_user(session, user_id, chat_id, coins):
    stat = UserStat(user_id=user_id, chat_id=chat_id, coins=coins)
    session.add(stat)
    await session.flush()
    return stat


# --- Тесты ---


def test_buy_ticket_success():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=50)
            result, balance = await buy_ticket(
                session, user_id=1, chat_id=100, user_name="Иван"
            )
            await session.commit()
            assert result is not None
            assert isinstance(result, LotteryTicket)
            assert result.coins_bet == TICKET_COST
            assert balance == 50 - TICKET_COST
        await engine.dispose()

    asyncio.run(_run())


def test_buy_ticket_not_enough_coins():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=5)
            result, reason = await buy_ticket(
                session, user_id=1, chat_id=100, user_name="Иван"
            )
            assert result is None
            assert "not_enough" in reason
        await engine.dispose()

    asyncio.run(_run())


def test_buy_multiple_tickets():
    """Один пользователь может купить несколько билетов в неделю."""
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=100)
            t1, _ = await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            t2, _ = await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            t3, bal = await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            await session.commit()
            assert t1 is not None
            assert t2 is not None
            assert t3 is not None
            assert bal == 100 - TICKET_COST * 3
        await engine.dispose()

    asyncio.run(_run())


def test_get_current_pot():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=100)
            await _create_user(session, user_id=2, chat_id=100, coins=100)
            await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            await buy_ticket(session, user_id=2, chat_id=100, user_name="Б")
            await session.commit()

            pot, participants, tickets = await get_current_pot(session, 100)
            assert pot == TICKET_COST * 3
            assert participants == 2
            assert tickets == 3
        await engine.dispose()

    asyncio.run(_run())


def test_draw_winner_not_enough_participants():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=100)
            await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            await session.commit()

            result = await draw_winner(session, 100)
            assert result is None
        await engine.dispose()

    asyncio.run(_run())


def test_draw_winner_success():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=100)
            await _create_user(session, user_id=2, chat_id=100, coins=100)
            await buy_ticket(session, user_id=1, chat_id=100, user_name="Иван")
            await buy_ticket(session, user_id=2, chat_id=100, user_name="Мария")
            await session.commit()

            result = await draw_winner(session, 100)
            await session.commit()
            assert result is not None
            assert result["prize"] == TICKET_COST * 2
            assert result["participants"] == 2
            assert result["winner_name"] in ("Иван", "Мария")

            # Проверяем что монеты начислены победителю
            winner_stats = await session.get(
                UserStat, {"user_id": result["winner_id"], "chat_id": 100}
            )
            expected_coins = (100 - TICKET_COST) + result["prize"]
            assert winner_stats.coins == expected_coins
        await engine.dispose()

    asyncio.run(_run())


def test_get_tickets_for_week():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=100)
            await buy_ticket(session, user_id=1, chat_id=100, user_name="А")
            await session.commit()
            wk = current_week_key()
            tickets = await get_tickets_for_week(session, 100, wk)
            assert len(tickets) == 1
            # Другая неделя — пусто
            tickets2 = await get_tickets_for_week(session, 100, "2000-W01")
            assert len(tickets2) == 0
        await engine.dispose()

    asyncio.run(_run())
