"""Почему: лотерея — еженедельное коллективное событие, куда жители тратят накопленные монеты."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LotteryTicket, UserStat

logger = logging.getLogger(__name__)

# Стоимость одного лотерейного билета
TICKET_COST = 50
# Минимальное число участников для розыгрыша (иначе переносится)
MIN_PARTICIPANTS = 2


def current_week_key() -> str:
    """Возвращает ключ текущей недели: 'YYYY-WNN'."""
    now = datetime.now(timezone.utc)
    return f"{now.year}-W{now.isocalendar()[1]:02d}"


async def buy_ticket(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    user_name: str | None,
) -> tuple[LotteryTicket, int] | tuple[None, str]:
    """Покупает лотерейный билет за TICKET_COST монет.
    Возвращает (ticket, new_balance) при успехе или (None, reason) при ошибке."""
    from sqlalchemy.exc import IntegrityError

    week_key = current_week_key()

    # Проверяем существующий билет
    existing = await session.execute(
        select(LotteryTicket).where(
            LotteryTicket.user_id == user_id,
            LotteryTicket.chat_id == chat_id,
            LotteryTicket.week_key == week_key,
        )
    )
    if existing.scalars().first() is not None:
        return None, "already_bought"

    # Проверяем баланс
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None or stats.coins < TICKET_COST:
        balance = stats.coins if stats else 0
        return None, f"not_enough:{balance}"

    stats.coins -= TICKET_COST
    ticket = LotteryTicket(
        user_id=user_id,
        chat_id=chat_id,
        user_name=user_name,
        coins_bet=TICKET_COST,
        week_key=week_key,
    )
    session.add(ticket)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None, "already_bought"

    return ticket, stats.coins


async def get_current_pot(session: AsyncSession, chat_id: int) -> tuple[int, int]:
    """Возвращает (сумма_банка, количество_участников) на текущей неделе."""
    week_key = current_week_key()
    tickets = (
        await session.execute(
            select(LotteryTicket).where(
                LotteryTicket.chat_id == chat_id,
                LotteryTicket.week_key == week_key,
            )
        )
    ).scalars().all()
    total = sum(t.coins_bet for t in tickets)
    return total, len(tickets)


async def draw_winner(
    session: AsyncSession,
    chat_id: int,
    week_key: str | None = None,
) -> dict | None:
    """Разыгрывает победителя за указанную неделю.
    Возвращает dict с winner_id, winner_name, prize или None если участников мало."""
    if week_key is None:
        week_key = current_week_key()

    tickets = (
        await session.execute(
            select(LotteryTicket).where(
                LotteryTicket.chat_id == chat_id,
                LotteryTicket.week_key == week_key,
            )
        )
    ).scalars().all()

    if len(tickets) < MIN_PARTICIPANTS:
        logger.info("LOTTERY: недостаточно участников (%d) для розыгрыша недели %s", len(tickets), week_key)
        return None

    # Взвешенная выборка: больше монет = больше шансов
    weights = [t.coins_bet for t in tickets]
    winner_ticket = random.choices(tickets, weights=weights, k=1)[0]
    prize = sum(t.coins_bet for t in tickets)

    # Начисляем приз победителю
    winner_stats = await session.get(UserStat, {"user_id": winner_ticket.user_id, "chat_id": chat_id})
    if winner_stats is not None:
        winner_stats.coins += prize
    else:
        from app.models import UserStat as US
        winner_stats = US(
            user_id=winner_ticket.user_id,
            chat_id=chat_id,
            coins=prize,
        )
        session.add(winner_stats)

    await session.flush()
    logger.info(
        "LOTTERY: победитель %s (%s), приз %d монет, неделя %s",
        winner_ticket.user_id, winner_ticket.user_name, prize, week_key,
    )
    return {
        "winner_id": winner_ticket.user_id,
        "winner_name": winner_ticket.user_name,
        "prize": prize,
        "participants": len(tickets),
        "week_key": week_key,
    }


async def get_tickets_for_week(
    session: AsyncSession,
    chat_id: int,
    week_key: str,
) -> list[LotteryTicket]:
    return (
        await session.execute(
            select(LotteryTicket).where(
                LotteryTicket.chat_id == chat_id,
                LotteryTicket.week_key == week_key,
            )
        )
    ).scalars().all()
