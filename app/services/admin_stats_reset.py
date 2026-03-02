"""Почему: централизуем безопасный сброс статистики без удаления базы знаний RAG."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameState, QuizDailyLimit, QuizSession, QuizUsedQuestion, QuizUserStat, UserStat


async def reset_runtime_statistics(session: AsyncSession) -> dict[str, int]:
    """Сбрасывает только оперативную статистику игр/викторины и оставляет RAG неизменным."""
    deleted_rows: dict[str, int] = {}

    game_stats_result = await session.execute(delete(UserStat))
    deleted_rows["user_stats"] = game_stats_result.rowcount or 0

    game_states_result = await session.execute(delete(GameState))
    deleted_rows["game_states"] = game_states_result.rowcount or 0

    quiz_stats_result = await session.execute(delete(QuizUserStat))
    deleted_rows["quiz_user_stats"] = quiz_stats_result.rowcount or 0

    quiz_limits_result = await session.execute(delete(QuizDailyLimit))
    deleted_rows["quiz_daily_limits"] = quiz_limits_result.rowcount or 0

    used_questions_result = await session.execute(delete(QuizUsedQuestion))
    deleted_rows["quiz_used_questions"] = used_questions_result.rowcount or 0

    quiz_sessions_result = await session.execute(delete(QuizSession))
    deleted_rows["quiz_sessions"] = quiz_sessions_result.rowcount or 0

    return deleted_rows
