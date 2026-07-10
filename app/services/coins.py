"""Почему: экономика монет отделена от игр — монеты живут в /магазин и /доработках,
даже когда игровые механики отключены. Будущие игры используют этот же модуль.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserStat


async def get_or_create_stats(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    display_name: str | None = None,
) -> UserStat:
    """Возвращает статистику пользователя (создаёт со 100 монетами при первом обращении)."""
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None:
        stats = UserStat(
            user_id=user_id, chat_id=chat_id, coins=100, display_name=display_name
        )
        session.add(stats)
        await session.flush()
    if display_name:
        stats.display_name = display_name
    return stats


def transfer_coins(sender: UserStat, receiver: UserStat, amount: int) -> str | None:
    """Перевод монет между пользователями. Возвращает None при успехе или текст ошибки."""
    if amount <= 0:
        return "Количество должно быть положительным числом."
    if sender.coins < amount:
        return "Недостаточно монет для подарка."
    sender.coins -= amount
    receiver.coins += amount
    return None
