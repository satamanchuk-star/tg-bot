"""Почему: централизуем безопасный сброс статистики без удаления базы знаний RAG.

Деньги игроков защищены: сброс — это UPDATE к дефолтному балансу (не DELETE строк),
активные ставки возвращаются перед удалением партий, история GameRound не трогается.
"""

from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameState, UserStat
from app.services.blackjack import refund_active_bets
from app.services.coins import DEFAULT_COINS


async def reset_runtime_statistics(session: AsyncSession) -> dict[str, int]:
    """Сбрасывает оперативную статистику: балансы к DEFAULT_COINS, счётчики в 0.

    Строки user_stats НЕ удаляются (display_name сохраняется), история партий
    GameRound не трогается — это вечный аудит. Активные ставки возвращаются
    на баланс до сброса, затем партии удаляются.
    """
    affected: dict[str, int] = {}

    # Сначала рефанд активных ставок (иначе админский сброс сожжёт деньги партий),
    # потом единый UPDATE балансов — рефанд «растворится» в дефолте, что и нужно:
    # смысл рефанда здесь — запись admin-закрытия в GameRound для аудита.
    affected["refunded_games"] = await refund_active_bets(session)

    stats_result = await session.execute(
        update(UserStat).values(
            coins=DEFAULT_COINS,
            games_played=0,
            wins=0,
            coins_granted_today=0,
            last_coin_grant_at=None,
        )
    )
    affected["user_stats"] = stats_result.rowcount or 0

    game_states_result = await session.execute(delete(GameState))
    affected["game_states"] = game_states_result.rowcount or 0

    return affected
