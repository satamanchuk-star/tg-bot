"""Почему: централизуем безопасный сброс статистики без удаления базы знаний RAG."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameState, UserStat


async def reset_runtime_statistics(session: AsyncSession) -> dict[str, int]:
    """Сбрасывает только оперативную статистику игр и оставляет RAG неизменным."""
    deleted_rows: dict[str, int] = {}

    game_stats_result = await session.execute(delete(UserStat))
    deleted_rows["user_stats"] = game_stats_result.rowcount or 0

    game_states_result = await session.execute(delete(GameState))
    deleted_rows["game_states"] = game_states_result.rowcount or 0

    return deleted_rows
