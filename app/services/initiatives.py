"""Почему: инициативы жителей — механизм коллективного улучшения ЖК за монеты."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Initiative, InitiativeVote, UserStat

logger = logging.getLogger(__name__)

# Сколько монет стоит создать инициативу
INITIATIVE_CREATE_COST = 50
# Сколько монет стоит проголосовать за чужую инициативу
INITIATIVE_VOTE_COST = 20
# Порог монет для объявления инициативы принятой
INITIATIVE_THRESHOLD = 300
# Максимум активных инициатив в одном чате одновременно
MAX_ACTIVE_INITIATIVES = 10


async def create_initiative(
    session: AsyncSession,
    *,
    chat_id: int,
    author_id: int,
    author_name: str | None,
    text: str,
) -> tuple[Initiative, int] | tuple[None, str]:
    """Создаёт инициативу, списывая INITIATIVE_CREATE_COST монет.
    Возвращает (initiative, new_balance) или (None, reason)."""
    # Проверяем баланс
    stats = await session.get(UserStat, {"user_id": author_id, "chat_id": chat_id})
    if stats is None or stats.coins < INITIATIVE_CREATE_COST:
        balance = stats.coins if stats else 0
        return None, f"not_enough:{balance}"

    # Ограничение на число активных инициатив
    active_count_result = await session.execute(
        select(Initiative).where(
            Initiative.chat_id == chat_id,
            Initiative.is_completed.is_(False),
        )
    )
    if len(active_count_result.scalars().all()) >= MAX_ACTIVE_INITIATIVES:
        return None, "too_many_active"

    stats.coins -= INITIATIVE_CREATE_COST
    # Автор автоматически вносит свои монеты в инициативу
    initiative = Initiative(
        chat_id=chat_id,
        author_id=author_id,
        author_name=author_name,
        text=text.strip()[:500],
        coins_total=INITIATIVE_CREATE_COST,
        threshold=INITIATIVE_THRESHOLD,
    )
    session.add(initiative)
    await session.flush()

    # Фиксируем голос самого автора
    vote = InitiativeVote(
        initiative_id=initiative.id,
        user_id=author_id,
        user_name=author_name,
        amount=INITIATIVE_CREATE_COST,
    )
    session.add(vote)
    await session.flush()

    logger.info("INITIATIVE: создана #%d от %s: %s", initiative.id, author_id, text[:60])
    return initiative, stats.coins


async def vote_for_initiative(
    session: AsyncSession,
    *,
    initiative_id: int,
    user_id: int,
    user_name: str | None,
    chat_id: int,
) -> tuple[Initiative, int, bool] | tuple[None, str, bool]:
    """Голосует за инициативу, тратя INITIATIVE_VOTE_COST монет.
    Возвращает (initiative, new_balance, just_completed) или (None, reason, False)."""
    initiative = await session.get(Initiative, initiative_id)
    if initiative is None or initiative.chat_id != chat_id:
        return None, "not_found", False
    if initiative.is_completed:
        return None, "already_completed", False

    # Нельзя голосовать за свою инициативу повторно (уже засчитано при создании)
    existing_vote = (
        await session.execute(
            select(InitiativeVote).where(
                InitiativeVote.initiative_id == initiative_id,
                InitiativeVote.user_id == user_id,
            )
        )
    ).scalars().first()
    if existing_vote is not None:
        return None, "already_voted", False

    # Проверяем баланс
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None or stats.coins < INITIATIVE_VOTE_COST:
        balance = stats.coins if stats else 0
        return None, f"not_enough:{balance}", False

    stats.coins -= INITIATIVE_VOTE_COST
    initiative.coins_total += INITIATIVE_VOTE_COST

    vote = InitiativeVote(
        initiative_id=initiative_id,
        user_id=user_id,
        user_name=user_name,
        amount=INITIATIVE_VOTE_COST,
    )
    session.add(vote)

    just_completed = False
    if initiative.coins_total >= initiative.threshold and not initiative.is_completed:
        initiative.is_completed = True
        just_completed = True
        logger.info(
            "INITIATIVE #%d достигла порога %d монет!", initiative_id, initiative.threshold
        )

    await session.flush()
    return initiative, stats.coins, just_completed


async def get_active_initiatives(
    session: AsyncSession,
    chat_id: int,
    *,
    limit: int = 10,
) -> list[Initiative]:
    """Возвращает активные инициативы, отсортированные по собранным монетам."""
    result = await session.execute(
        select(Initiative)
        .where(
            Initiative.chat_id == chat_id,
            Initiative.is_completed.is_(False),
        )
        .order_by(Initiative.coins_total.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_initiative(session: AsyncSession, initiative_id: int) -> Initiative | None:
    return await session.get(Initiative, initiative_id)
