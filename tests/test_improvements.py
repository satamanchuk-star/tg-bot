"""Тесты доработок бота: создание, голосование, лимиты."""

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import BotImprovement, ImprovementVote, UserStat
from app.services.improvements import (
    IMPROVEMENT_CREATE_COST,
    IMPROVEMENT_THRESHOLD,
    IMPROVEMENT_VOTE_COST,
    can_create_improvement_this_month,
    create_improvement,
    get_active_improvements,
    vote_for_improvement,
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


def test_create_improvement_success():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=200)
            result, balance = await create_improvement(
                session,
                chat_id=100,
                author_id=1,
                author_name="Иван",
                text="Добавить уведомления о доставке посылок",
            )
            await session.commit()
            assert result is not None
            assert isinstance(result, BotImprovement)
            assert result.coins_total == IMPROVEMENT_CREATE_COST
            assert result.threshold == IMPROVEMENT_THRESHOLD
            assert result.is_completed is False
            assert result.expires_at > datetime.now(timezone.utc)
            assert balance == 200 - IMPROVEMENT_CREATE_COST
        await engine.dispose()

    asyncio.run(_run())


def test_create_improvement_not_enough_coins():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=10)
            result, reason = await create_improvement(
                session,
                chat_id=100,
                author_id=1,
                author_name="Иван",
                text="Добавить погоду в чат",
            )
            assert result is None
            assert "not_enough" in reason
        await engine.dispose()

    asyncio.run(_run())


def test_monthly_limit():
    """Пользователь может подать только 1 доработку в месяц."""
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=500)

            # Первая — успех
            r1, _ = await create_improvement(
                session, chat_id=100, author_id=1, author_name="Иван",
                text="Первая доработка — уведомления",
            )
            await session.commit()
            assert r1 is not None

            # Вторая в том же месяце — должна быть заблокирована
            can = await can_create_improvement_this_month(session, 1, 100)
            assert can is False
        await engine.dispose()

    asyncio.run(_run())


def test_vote_success():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=200)
            await _create_user(session, user_id=2, chat_id=100, coins=100)

            imp, _ = await create_improvement(
                session, chat_id=100, author_id=1, author_name="Иван",
                text="Добавить расписание вывоза мусора",
            )
            await session.commit()
            old_total = imp.coins_total

            result, balance, just_completed = await vote_for_improvement(
                session,
                improvement_id=imp.id,
                user_id=2,
                user_name="Мария",
                chat_id=100,
            )
            await session.commit()
            assert result is not None
            assert result.coins_total == old_total + IMPROVEMENT_VOTE_COST
            assert balance == 100 - IMPROVEMENT_VOTE_COST
            assert just_completed is False
        await engine.dispose()

    asyncio.run(_run())


def test_vote_duplicate_blocked():
    """Один пользователь не может голосовать за одну доработку дважды."""
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=200)
            await _create_user(session, user_id=2, chat_id=100, coins=100)

            imp, _ = await create_improvement(
                session, chat_id=100, author_id=1, author_name="Иван",
                text="Добавить бронирование парковки",
            )
            await session.commit()

            r1, _, _ = await vote_for_improvement(
                session, improvement_id=imp.id, user_id=2,
                user_name="Мария", chat_id=100,
            )
            await session.commit()
            assert r1 is not None

            r2, reason, _ = await vote_for_improvement(
                session, improvement_id=imp.id, user_id=2,
                user_name="Мария", chat_id=100,
            )
            assert r2 is None
            assert reason == "already_voted"
        await engine.dispose()

    asyncio.run(_run())


def test_completion_at_threshold():
    """Доработка принимается когда набирается порог монет."""
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            # Ставим порог = 80 чтобы быстро достичь
            await _create_user(session, user_id=1, chat_id=100, coins=500)
            imp, _ = await create_improvement(
                session, chat_id=100, author_id=1, author_name="Иван",
                text="Мини-доработка для теста",
            )
            imp.threshold = 80  # Порог: 50 (создание) + 10 + 10 + 10 = 80
            await session.commit()

            for voter_id in [10, 11, 12]:
                await _create_user(session, voter_id, 100, 50)
            await session.commit()

            # Третий голос достигает порога
            for i, voter_id in enumerate([10, 11, 12]):
                result, _, just_completed = await vote_for_improvement(
                    session, improvement_id=imp.id, user_id=voter_id,
                    user_name=f"Голосующий {voter_id}", chat_id=100,
                )
                await session.commit()

            # Последний голос должен был завершить
            assert result is not None
            assert result.is_completed is True
            assert just_completed is True
        await engine.dispose()

    asyncio.run(_run())


def test_get_active_improvements():
    async def _run():
        engine, factory = _make_session_factory()
        await _setup_db(engine)
        async with factory() as session:
            await _create_user(session, user_id=1, chat_id=100, coins=500)
            # Создаём вручную для теста (обходим лимит 1/месяц)
            from datetime import timedelta
            for i in range(3):
                imp = BotImprovement(
                    chat_id=100, author_id=1, author_name="Иван",
                    text=f"Доработка {i}", coins_total=i * 10,
                    threshold=500,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                )
                session.add(imp)
            await session.commit()

            active = await get_active_improvements(session, 100)
            assert len(active) == 3
            # Отсортированы по coins_total DESC
            assert active[0].coins_total >= active[1].coins_total
        await engine.dispose()

    asyncio.run(_run())
