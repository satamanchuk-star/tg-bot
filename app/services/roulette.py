"""Почему: бизнес-логика рулетки вынесена отдельно от хендлеров для переиспользования и тестов."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RouletteBet, RouletteRound, RouletteUserStat, UserStat

# Максимум ставок на раунд для одного пользователя
MAX_BETS_PER_ROUND = 3

# Время приёма ставок в секундах
BETTING_DURATION_SEC = 120

# Таблица цветов рулетки: 0 = зелёный
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}


def get_number_color(number: int) -> str:
    """Возвращает цвет числа: red, black или green (для 0)."""
    if number == 0:
        return "green"
    if number in RED_NUMBERS:
        return "red"
    return "black"


def get_number_parity(number: int) -> str | None:
    """Возвращает чётность числа: even, odd или None для 0."""
    if number == 0:
        return None
    return "even" if number % 2 == 0 else "odd"


def color_emoji(color: str) -> str:
    if color == "red":
        return "🔴"
    if color == "black":
        return "⚫"
    return "🟢"


def color_name_ru(color: str) -> str:
    if color == "red":
        return "красное"
    if color == "black":
        return "чёрное"
    return "зелёное (зеро)"


def parity_name_ru(parity: str | None) -> str:
    if parity == "even":
        return "чёт"
    if parity == "odd":
        return "нечёт"
    return "—"


# Маппинг алиасов ставок (русский/английский)
BET_TYPE_ALIASES: dict[str, tuple[str, str]] = {
    # (bet_type, bet_value)
    "red": ("color", "red"),
    "красное": ("color", "red"),
    "красный": ("color", "red"),
    "black": ("color", "black"),
    "чёрное": ("color", "black"),
    "черное": ("color", "black"),
    "чёрный": ("color", "black"),
    "черный": ("color", "black"),
    "even": ("parity", "even"),
    "чёт": ("parity", "even"),
    "чет": ("parity", "even"),
    "четное": ("parity", "even"),
    "чётное": ("parity", "even"),
    "odd": ("parity", "odd"),
    "нечёт": ("parity", "odd"),
    "нечет": ("parity", "odd"),
    "нечетное": ("parity", "odd"),
    "нечётное": ("parity", "odd"),
}


def parse_bet(raw_type: str) -> tuple[str, str] | None:
    """Парсит тип ставки. Возвращает (bet_type, bet_value) или None."""
    lower = raw_type.lower().strip()
    if lower in BET_TYPE_ALIASES:
        return BET_TYPE_ALIASES[lower]
    # Проверка на число 0-36
    try:
        num = int(lower)
        if 0 <= num <= 36:
            return ("number", str(num))
    except ValueError:
        pass
    return None


def calculate_winnings(bet_type: str, bet_value: str, amount: int, result_number: int) -> int:
    """Рассчитывает выигрыш. Возвращает 0, если ставка проиграла."""
    result_color = get_number_color(result_number)
    result_parity = get_number_parity(result_number)

    if bet_type == "color":
        if bet_value == result_color:
            return amount * 2  # 1:1 + возврат ставки
        return 0

    if bet_type == "parity":
        if result_number == 0:
            return 0  # 0 — проигрыш для чёт/нечёт
        if bet_value == result_parity:
            return amount * 2
        return 0

    if bet_type == "number":
        if int(bet_value) == result_number:
            return amount * 36  # 35:1 + возврат ставки
        return 0

    return 0


# --- Работа с БД ---

async def create_round(session: AsyncSession, chat_id: int, topic_id: int) -> RouletteRound:
    """Создаёт новый активный раунд."""
    rnd = RouletteRound(chat_id=chat_id, topic_id=topic_id, is_active=True)
    session.add(rnd)
    await session.flush()
    return rnd


async def get_active_round(session: AsyncSession, chat_id: int, topic_id: int) -> RouletteRound | None:
    """Получает текущий активный раунд."""
    result = await session.execute(
        select(RouletteRound).where(
            RouletteRound.chat_id == chat_id,
            RouletteRound.topic_id == topic_id,
            RouletteRound.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def close_round(session: AsyncSession, rnd: RouletteRound, result_number: int) -> None:
    """Закрывает раунд с результатом."""
    rnd.result_number = result_number
    rnd.is_active = False
    await session.flush()


async def get_user_bets_count(session: AsyncSession, round_id: int, user_id: int) -> int:
    """Количество ставок пользователя в раунде."""
    result = await session.execute(
        select(RouletteBet).where(
            RouletteBet.round_id == round_id,
            RouletteBet.user_id == user_id,
        )
    )
    return len(result.scalars().all())


async def place_bet(
    session: AsyncSession,
    round_id: int,
    user_id: int,
    bet_type: str,
    bet_value: str,
    amount: int,
    display_name: str | None = None,
) -> RouletteBet:
    """Размещает ставку."""
    bet = RouletteBet(
        round_id=round_id,
        user_id=user_id,
        bet_type=bet_type,
        bet_value=bet_value,
        amount=amount,
        display_name=display_name,
    )
    session.add(bet)
    await session.flush()
    return bet


async def get_round_bets(session: AsyncSession, round_id: int) -> list[RouletteBet]:
    """Все ставки раунда."""
    result = await session.execute(
        select(RouletteBet).where(RouletteBet.round_id == round_id)
    )
    return list(result.scalars().all())


async def get_or_create_user_stats(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
) -> UserStat:
    """Получает или создаёт запись баланса пользователя."""
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None:
        stats = UserStat(user_id=user_id, chat_id=chat_id, coins=100)
        session.add(stats)
        await session.flush()
    return stats


async def deduct_coins(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    amount: int,
) -> UserStat | None:
    """Списывает монеты. Возвращает None, если недостаточно средств."""
    stats = await get_or_create_user_stats(session, user_id, chat_id)
    if stats.coins < amount:
        return None
    stats.coins -= amount
    await session.flush()
    return stats


async def credit_coins(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    amount: int,
    display_name: str | None = None,
) -> UserStat:
    """Зачисляет монеты на баланс."""
    stats = await get_or_create_user_stats(session, user_id, chat_id)
    stats.coins += amount
    if display_name:
        stats.display_name = display_name
    await session.flush()
    return stats


async def update_roulette_stats(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    won: int,
    lost: int,
    display_name: str | None = None,
) -> None:
    """Обновляет общую статистику рулетки пользователя."""
    stat = await session.get(RouletteUserStat, {"user_id": user_id, "chat_id": chat_id})
    if stat is None:
        stat = RouletteUserStat(
            user_id=user_id, chat_id=chat_id, total_won=won, total_lost=lost, display_name=display_name
        )
        session.add(stat)
    else:
        stat.total_won += won
        stat.total_lost += lost
        if display_name:
            stat.display_name = display_name
    await session.flush()


def spin_wheel() -> int:
    """Генерирует случайное число 0-36."""
    return random.randint(0, 36)


def format_bet_description(bet_type: str, bet_value: str) -> str:
    """Человекочитаемое описание ставки."""
    if bet_type == "color":
        return color_name_ru(bet_value)
    if bet_type == "parity":
        return parity_name_ru(bet_value)
    if bet_type == "number":
        return f"число {bet_value}"
    return bet_value
