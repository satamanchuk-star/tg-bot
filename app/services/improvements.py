"""Почему: доработки бота — механизм коллективного развития бота за монеты."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotImprovement, ImprovementVote, UserStat

logger = logging.getLogger(__name__)

# Стоимость создания доработки
IMPROVEMENT_CREATE_COST = 50
# Стоимость голоса за чужую доработку
IMPROVEMENT_VOTE_COST = 10
# Порог монет для принятия доработки в работу
IMPROVEMENT_THRESHOLD = 500
# Срок голосования (дней)
IMPROVEMENT_LIFETIME_DAYS = 7


async def can_create_improvement_this_month(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
) -> bool:
    """Возвращает True если пользователь НЕ создавал доработку в текущем месяце."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    existing = (
        await session.execute(
            select(BotImprovement).where(
                BotImprovement.chat_id == chat_id,
                BotImprovement.author_id == user_id,
                BotImprovement.created_at >= month_start,
            )
        )
    ).scalars().first()
    return existing is None


async def create_improvement(
    session: AsyncSession,
    *,
    chat_id: int,
    author_id: int,
    author_name: str | None,
    text: str,
) -> tuple[BotImprovement, int] | tuple[None, str]:
    """Создаёт доработку, списывая IMPROVEMENT_CREATE_COST монет.
    Возвращает (improvement, new_balance) или (None, reason)."""
    stats = await session.get(UserStat, {"user_id": author_id, "chat_id": chat_id})
    if stats is None or stats.coins < IMPROVEMENT_CREATE_COST:
        balance = stats.coins if stats else 0
        return None, f"not_enough:{balance}"

    stats.coins -= IMPROVEMENT_CREATE_COST
    now = datetime.now(timezone.utc)
    improvement = BotImprovement(
        chat_id=chat_id,
        author_id=author_id,
        author_name=author_name,
        text=text.strip()[:1000],
        coins_total=IMPROVEMENT_CREATE_COST,
        threshold=IMPROVEMENT_THRESHOLD,
        expires_at=now + timedelta(days=IMPROVEMENT_LIFETIME_DAYS),
    )
    session.add(improvement)
    await session.flush()

    # Автор сразу вносит свои монеты в банк доработки
    vote = ImprovementVote(
        improvement_id=improvement.id,
        user_id=author_id,
        user_name=author_name,
        amount=IMPROVEMENT_CREATE_COST,
    )
    session.add(vote)
    await session.flush()

    logger.info("IMPROVEMENT: создана #%d от %s: %s", improvement.id, author_id, text[:60])
    return improvement, stats.coins


async def vote_for_improvement(
    session: AsyncSession,
    *,
    improvement_id: int,
    user_id: int,
    user_name: str | None,
    chat_id: int,
) -> tuple[BotImprovement, int, bool] | tuple[None, str, bool]:
    """Голосует за доработку, тратя IMPROVEMENT_VOTE_COST монет.
    Возвращает (improvement, new_balance, just_completed) или (None, reason, False)."""
    improvement = await session.get(BotImprovement, improvement_id)
    if improvement is None or improvement.chat_id != chat_id:
        return None, "not_found", False
    if improvement.is_completed:
        return None, "already_completed", False
    expires_at = improvement.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None, "expired", False

    existing_vote = (
        await session.execute(
            select(ImprovementVote).where(
                ImprovementVote.improvement_id == improvement_id,
                ImprovementVote.user_id == user_id,
            )
        )
    ).scalars().first()
    if existing_vote is not None:
        return None, "already_voted", False

    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None or stats.coins < IMPROVEMENT_VOTE_COST:
        balance = stats.coins if stats else 0
        return None, f"not_enough:{balance}", False

    stats.coins -= IMPROVEMENT_VOTE_COST
    improvement.coins_total += IMPROVEMENT_VOTE_COST

    vote = ImprovementVote(
        improvement_id=improvement_id,
        user_id=user_id,
        user_name=user_name,
        amount=IMPROVEMENT_VOTE_COST,
    )
    session.add(vote)

    just_completed = False
    if improvement.coins_total >= improvement.threshold and not improvement.is_completed:
        improvement.is_completed = True
        just_completed = True
        logger.info("IMPROVEMENT #%d принята в работу! %d монет", improvement_id, improvement.coins_total)

    await session.flush()
    return improvement, stats.coins, just_completed


async def get_active_improvements(
    session: AsyncSession,
    chat_id: int,
    *,
    limit: int = 10,
) -> list[BotImprovement]:
    """Возвращает активные (не истёкшие, не принятые) доработки, сортированные по монетам."""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(BotImprovement)
        .where(
            BotImprovement.chat_id == chat_id,
            BotImprovement.is_completed.is_(False),
            BotImprovement.expires_at > now,
        )
        .order_by(BotImprovement.coins_total.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_improvement(session: AsyncSession, improvement_id: int) -> BotImprovement | None:
    return await session.get(BotImprovement, improvement_id)
