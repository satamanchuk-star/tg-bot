"""Тесты сервиса «21»: ставки, развязка, экономика, миграция, защита денег."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, GameRound, GameState, UserStat


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


def test_place_bet_and_deal_atomic(db) -> None:
    from app.services import blackjack as bj

    async def _run():
        async with db() as session:
            state = bj.new_betting_state()
            await bj.save_game(session, 1, 10, state)
            state, reason = await bj.place_bet_and_deal(session, 1, 10, 25, "Вася")
            await session.commit()
            stats = await session.get(UserStat, {"user_id": 1, "chat_id": 10})
            return state, reason, stats

    state, reason, stats = asyncio.run(_run())
    assert reason is None and state is not None
    assert state.phase == "playing" and state.bet == 25
    assert len(state.player_hand) == 2 and len(state.dealer_hand) == 2
    assert len(state.deck) == 48
    assert stats.coins == 200 - 25  # дефолт 200, ставка списана
    assert stats.games_played == 1


def test_bet_rejected_without_deduction_when_poor(db) -> None:
    from app.services import blackjack as bj
    from app.services.coins import get_or_create_stats

    async def _run():
        async with db() as session:
            stats = await get_or_create_stats(session, 1, 10)
            stats.coins = 3  # меньше минимальной ставки
            await bj.save_game(session, 1, 10, bj.new_betting_state())
            state, reason = await bj.place_bet_and_deal(session, 1, 10, 5)
            await session.commit()
            return state, reason, stats.coins

    state, reason, coins = asyncio.run(_run())
    assert state is None and "Не хватает" in reason
    assert coins == 3  # баланс не тронут


def test_double_bet_click_rejected(db) -> None:
    from app.services import blackjack as bj

    async def _run():
        async with db() as session:
            await bj.save_game(session, 1, 10, bj.new_betting_state())
            first, r1 = await bj.place_bet_and_deal(session, 1, 10, 5)
            second, r2 = await bj.place_bet_and_deal(session, 1, 10, 5)
            await session.commit()
            stats = await session.get(UserStat, {"user_id": 1, "chat_id": 10})
            return first, r1, second, r2, stats

    first, r1, second, r2, stats = asyncio.run(_run())
    assert first is not None and r1 is None
    assert second is None and r2 == "Ставка уже сделана."  # идемпотентность
    assert stats.coins == 195  # списано один раз


def test_settle_records_round_and_pays(db, monkeypatch) -> None:
    """Развязка на подложенной колоде: победа игрока → выплата ×2, wins+1, история."""
    from app.handlers.blackjack import _settle
    from app.services import blackjack as bj

    async def _run():
        async with db() as session:
            await bj.save_game(session, 1, 10, bj.new_betting_state())
            state, _ = await bj.place_bet_and_deal(session, 1, 10, 25)
            # Подкладываем гарантированную победу: у игрока 20, у дилера 19.
            state.player_hand = ["К♠", "Д♥"]
            state.dealer_hand = ["10♦", "9♣"]
            state.deck = ["2♠"]  # дилеру хватает 19 — не доберёт
            result, payout, balance = await _settle(session, 1, 10, state)
            await session.commit()
            rounds = (await session.execute(select(GameRound))).scalars().all()
            games = (await session.execute(select(GameState))).scalars().all()
            stats = await session.get(UserStat, {"user_id": 1, "chat_id": 10})
            return result, payout, balance, rounds, games, stats

    result, payout, balance, rounds, games, stats = asyncio.run(_run())
    assert result == "win" and payout == 50
    assert balance == 200 - 25 + 50
    assert stats.wins == 1 and stats.games_played == 1
    assert len(rounds) == 1
    assert rounds[0].bet == 25 and rounds[0].payout == 50 and rounds[0].closed_by == "player"
    assert games == []  # партия удалена


def test_rescue_if_bankrupt_thresholds() -> None:
    from app.services.coins import rescue_if_bankrupt

    s0 = UserStat(user_id=1, chat_id=1, coins=0)
    assert rescue_if_bankrupt(s0, 5, 10) is True and s0.coins == 10
    s4 = UserStat(user_id=2, chat_id=1, coins=4)
    assert rescue_if_bankrupt(s4, 5, 10) is True and s4.coins == 10
    s5 = UserStat(user_id=3, chat_id=1, coins=5)
    assert rescue_if_bankrupt(s5, 5, 10) is False and s5.coins == 5


def test_daily_bonus_once_per_day_and_naive_safe() -> None:
    from app.services.coins import DAILY_BONUS, try_grant_daily_bonus

    now = datetime.now(timezone.utc)
    stats = UserStat(user_id=1, chat_id=1, coins=100)
    assert try_grant_daily_bonus(stats, now) is True
    assert stats.coins == 100 + DAILY_BONUS
    assert try_grant_daily_bonus(stats, now) is False  # второй раз в день — нет

    # Naive datetime из SQLite не роняет (прод-регресс TypeError).
    stats.last_coin_grant_at = (now - timedelta(hours=2)).replace(tzinfo=None)
    assert try_grant_daily_bonus(stats, now) is False

    stats.last_coin_grant_at = (now - timedelta(days=1, minutes=1)).replace(tzinfo=None)
    assert try_grant_daily_bonus(stats, now) is True  # новый день


def test_get_or_create_gives_200(db) -> None:
    from app.services.coins import get_or_create_stats

    async def _run():
        async with db() as session:
            stats = await get_or_create_stats(session, 7, 10, "Петя")
            await session.commit()
            return stats.coins

    assert asyncio.run(_run()) == 200


def test_reset_stats_updates_not_deletes(db) -> None:
    """Сброс: балансы к 200 UPDATE'ом, display_name и история партий сохраняются."""
    from app.services.admin_stats_reset import reset_runtime_statistics

    async def _run():
        async with db() as session:
            session.add(UserStat(user_id=1, chat_id=10, coins=999, games_played=7,
                                 wins=3, display_name="Вася"))
            session.add(GameRound(user_id=1, chat_id=10, bet=5, result="win", payout=10,
                                  player_hand="К♥ Д♠", dealer_hand="9♦ 8♣"))
            await session.commit()
            affected = await reset_runtime_statistics(session)
            await session.commit()
            stats = await session.get(UserStat, {"user_id": 1, "chat_id": 10})
            rounds = (await session.execute(select(GameRound))).scalars().all()
            return affected, stats, rounds

    affected, stats, rounds = asyncio.run(_run())
    assert affected["user_stats"] == 1
    assert stats is not None  # строка НЕ удалена
    assert stats.coins == 200 and stats.games_played == 0 and stats.wins == 0
    assert stats.display_name == "Вася"
    assert len(rounds) == 1  # история — вечный аудит


def test_refund_active_bets_returns_money(db) -> None:
    """Принудительная чистка (restart_jobs/reset_stats) возвращает активные ставки."""
    from app.services import blackjack as bj

    async def _run():
        async with db() as session:
            await bj.save_game(session, 1, 10, bj.new_betting_state())
            state, _ = await bj.place_bet_and_deal(session, 1, 10, 50)
            await session.commit()
            refunded = await bj.refund_active_bets(session)
            await session.commit()
            stats = await session.get(UserStat, {"user_id": 1, "chat_id": 10})
            rounds = (await session.execute(select(GameRound))).scalars().all()
            return refunded, stats.coins, rounds

    refunded, coins, rounds = asyncio.run(_run())
    assert refunded == 1
    assert coins == 200  # ставка вернулась
    assert rounds[0].closed_by == "admin" and rounds[0].result == "push"


def test_migration_v12_sets_200_once(db, monkeypatch) -> None:
    from app.main import apply_v12_coins_200

    async def _run():
        async with db() as session:
            session.add(UserStat(user_id=1, chat_id=10, coins=37))
            await session.commit()
            await apply_v12_coins_200(session)
            stats = await session.get(UserStat, {"user_id": 1, "chat_id": 10})
            first = stats.coins
            # Повторный вызов при установленном флаге ничего не меняет.
            stats.coins = 50
            await session.commit()
            await apply_v12_coins_200(session)
            await session.refresh(stats)
            return first, stats.coins

    first, second = asyncio.run(_run())
    assert first == 200
    assert second == 50
