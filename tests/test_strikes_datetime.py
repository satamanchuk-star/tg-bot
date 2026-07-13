"""Регресс: страйк за мат не должен падать на naive datetime из SQLite.

Ошибка в проде: TypeError can't subtract offset-naive and offset-aware
datetimes — SQLite возвращает created_at без tzinfo, а now() был aware.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Strike
from app.services.strikes import STRIKE_RESET_DAYS, add_strike


def _make_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())
    return engine, factory


def test_add_strike_survives_naive_created_at() -> None:
    """add_strike не падает, когда в БД лежит naive datetime."""
    engine, factory = _make_factory()

    async def _run() -> int:
        async with factory() as session:
            # Свежий страйк с naive-временем (как отдаёт SQLite).
            session.add(Strike(user_id=1, chat_id=10, created_at=datetime.utcnow()))
            await session.commit()
            # Раньше эта строка бросала TypeError.
            return await add_strike(session, user_id=1, chat_id=10)

    count = asyncio.run(_run())
    assert count == 2
    asyncio.run(engine.dispose())


def test_add_strike_resets_after_window_with_naive_dt() -> None:
    """Старые (>30 дней) naive-страйки сбрасываются, а не роняют обработчик."""
    engine, factory = _make_factory()

    async def _run() -> int:
        async with factory() as session:
            old = datetime.utcnow() - timedelta(days=STRIKE_RESET_DAYS + 1)
            session.add(Strike(user_id=2, chat_id=10, created_at=old))
            await session.commit()
            return await add_strike(session, user_id=2, chat_id=10)

    # Старый страйк удалён, остаётся только новый → счётчик 1.
    count = asyncio.run(_run())
    assert count == 1
    asyncio.run(engine.dispose())
