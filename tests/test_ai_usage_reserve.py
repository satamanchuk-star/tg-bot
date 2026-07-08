"""Тесты атомарного резервирования дневного лимита AI-запросов."""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.services.ai_usage import add_tokens, get_usage_stats, try_reserve_request


async def _run_reserve_scenario() -> tuple[list[bool], int, int]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    results: list[bool] = []
    async with session_factory() as session:
        # Лимит 3 запроса: первые три резерва проходят, четвёртый — отказ.
        for _ in range(4):
            allowed, _reason = await try_reserve_request(
                session, date_key="2026-07-08", chat_id=42,
                request_limit=3, token_limit=0,
            )
            results.append(allowed)

        # Токены дописываются к уже зарезервированным запросам.
        await add_tokens(session, date_key="2026-07-08", chat_id=42, tokens_used=150)
        stats = await get_usage_stats(session, date_key="2026-07-08", chat_id=42)

    await engine.dispose()
    return results, stats.requests_used, stats.tokens_used


def test_reserve_enforces_request_limit_atomically() -> None:
    results, requests_used, tokens_used = asyncio.run(_run_reserve_scenario())
    assert results == [True, True, True, False]
    assert requests_used == 3  # отказ не инкрементирует счётчик
    assert tokens_used == 150


async def _run_token_limit_scenario() -> bool:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        allowed, _ = await try_reserve_request(
            session, date_key="2026-07-08", chat_id=7,
            request_limit=100, token_limit=100,
        )
        assert allowed
        await add_tokens(session, date_key="2026-07-08", chat_id=7, tokens_used=100)
        # Токен-лимит исчерпан — следующий резерв должен отклониться.
        allowed, _ = await try_reserve_request(
            session, date_key="2026-07-08", chat_id=7,
            request_limit=100, token_limit=100,
        )
    await engine.dispose()
    return allowed


def test_reserve_respects_token_limit() -> None:
    assert asyncio.run(_run_token_limit_scenario()) is False
