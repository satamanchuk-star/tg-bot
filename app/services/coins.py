"""Почему: экономика монет отделена от игр — монеты живут своей жизнью,
даже когда игровые механики отключены. Игры (блэкджек) используют этот модуль.

Гарантия персистентности: баланс хранится в user_stats (SQLite + ночной бэкап),
рестарты/деплои его не трогают. Единственные пути изменения — ставки/выплаты,
/подарить, /бонус, /addcoins и осознанный админский /reset_stats (UPDATE к дефолту).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserStat
from app.utils.time import ensure_aware

# Стартовый и «сбросовый» баланс. Единая константа: default в models.UserStat
# держим синхронным с ней (двойное место — иначе разъедутся).
DEFAULT_COINS = 200

# Ежедневный бонус (/бонус): +10 монет раз в сутки.
DAILY_BONUS = 10


async def get_or_create_stats(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    display_name: str | None = None,
) -> UserStat:
    """Возвращает статистику пользователя (создаёт с DEFAULT_COINS при первом обращении)."""
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None:
        stats = UserStat(
            user_id=user_id, chat_id=chat_id, coins=DEFAULT_COINS, display_name=display_name
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


def try_grant_daily_bonus(stats: UserStat, now: datetime) -> bool:
    """Ежедневный бонус +DAILY_BONUS: True, если начислен; False, если сегодня уже был.

    Переиспользует поля last_coin_grant_at/coins_granted_today. SQLite отдаёт
    datetime без tzinfo — сравнение только через ensure_aware (была прод-ошибка
    TypeError на вычитании naive/aware).
    """
    if stats.last_coin_grant_at is not None:
        last = ensure_aware(stats.last_coin_grant_at)
        if now - last < timedelta(days=1):
            return False
    stats.coins += DAILY_BONUS
    stats.last_coin_grant_at = now
    stats.coins_granted_today = DAILY_BONUS
    return True


def rescue_if_bankrupt(stats: UserStat, min_balance: int, top_up_to: int) -> bool:
    """Спасение банкрота: баланс < min_balance → пополнение до top_up_to.

    Пороги передаёт вызывающий (блэкджек: MIN_BET и BANKRUPT_TOP_UP) — без
    обратного импорта игрового модуля. True, если пополнение сработало.
    """
    if stats.coins < min_balance:
        stats.coins = top_up_to
        return True
    return False
