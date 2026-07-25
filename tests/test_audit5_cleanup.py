"""Регрессии аудита-5: чистка мёртвых таблиц волнами, общие примитивы игр."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import MigrationFlag


def test_drop_orphaned_v3_runs_even_when_v2_flag_set() -> None:
    """На живом проде флаг v2 уже стоит — волна v3 всё равно должна удалить
    таблицы магазина/доработок (ранний return по v2 не блокирует v3)."""
    from app.main import drop_orphaned_tables

    async def scenario() -> tuple[list, bool]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            # Имитация прода: v2 уже отработала, мёртвая таблица существует
            session.add(MigrationFlag(key="drop_orphaned_tables_v2"))
            await session.execute(text("CREATE TABLE bot_improvements (id INTEGER)"))
            await session.commit()

            await drop_orphaned_tables(session)

            leftover = (await session.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('bot_improvements','improvement_votes','shop_purchases')"
            ))).all()
            v3 = await session.get(MigrationFlag, "drop_orphaned_tables_v3")
        await engine.dispose()
        return leftover, v3 is not None

    leftover, v3_set = asyncio.run(scenario())
    assert leftover == []
    assert v3_set


def test_dead_shop_modules_are_gone() -> None:
    """Мёртвый функционал удалён целиком — и код, и модели."""
    import importlib
    import pytest

    for module in ("app.handlers.shop", "app.handlers.economy",
                   "app.services.shop", "app.services.improvements"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    import app.models as m
    for name in ("BotImprovement", "ImprovementVote", "ShopPurchase"):
        assert not hasattr(m, name)


def test_game_common_lock_registry() -> None:
    """Один ключ → один и тот же lock; clear сбрасывает."""
    from app.services.game_common import LockRegistry

    reg = LockRegistry()
    assert reg.for_key(1) is reg.for_key(1)
    assert reg.for_key(1) is not reg.for_key(2)
    reg.clear()
    # После сброса — новый объект лока
    old = reg.for_key(3)
    reg.clear()
    assert reg.for_key(3) is not old


def test_both_games_share_common_primitives() -> None:
    """Хендлеры «21» и викторины используют ОДНИ реализации примитивов."""
    from app.handlers import blackjack as b
    from app.handlers import quiz as qh
    from app.services import game_common as gc

    assert b._in_games_topic is gc.in_games_topic
    assert qh._in_games_topic is gc.in_games_topic
    assert b._display_name is qh._display_name is gc.display_name
    assert qh._safe_react is gc.safe_react
    assert b._safe_answer is gc.safe_answer
