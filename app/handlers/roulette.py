"""Почему: логика взаимодействия с пользователем в рулетке изолирована в отдельном роутере."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.roulette import (
    BETTING_DURATION_SEC,
    MAX_BETS_PER_ROUND,
    calculate_winnings,
    close_round,
    color_emoji,
    color_name_ru,
    create_round,
    credit_coins,
    deduct_coins,
    format_bet_description,
    get_active_round,
    get_number_color,
    get_number_parity,
    get_or_create_user_stats,
    get_round_bets,
    get_user_bets_count,
    parity_name_ru,
    parse_bet,
    place_bet,
    spin_wheel,
    update_roulette_stats,
)
from app.utils.time import is_game_time_allowed

logger = logging.getLogger(__name__)
router = Router()

# Лок для защиты от гонок при размещении ставок
_bet_lock = asyncio.Lock()

# Задача текущего раунда (чтобы не запускать дубли)
_round_task: asyncio.Task | None = None


def _is_roulette_time() -> bool:
    """Рулетка доступна с 21:00 до 22:00 МСК."""
    return is_game_time_allowed(21, 22)


def _is_in_game_topic(message: Message) -> bool:
    if settings.topic_games is None:
        return False
    return (
        message.chat.id == settings.forum_chat_id
        and message.message_thread_id == settings.topic_games
    )


@router.message(Command("bet"))
async def handle_bet(message: Message) -> None:
    """Обработчик команды /bet <тип> <сумма>."""
    if not _is_in_game_topic(message):
        return
    if message.from_user is None:
        return

    if not _is_roulette_time():
        await message.reply("Рулетка работает с 21:00 до 22:00 по Москве.")
        return

    # Парсим аргументы: /bet <тип> <сумма>
    args = (message.text or "").split()
    if len(args) < 3:
        await message.reply(
            "Формат: /bet <тип> <сумма>\n"
            "Типы: red/красное, black/чёрное, even/чёт, odd/нечёт, число (0-36)\n"
            "Пример: /bet red 50"
        )
        return

    raw_type = args[1]
    parsed = parse_bet(raw_type)
    if parsed is None:
        await message.reply(
            "Неизвестный тип ставки. Доступны:\n"
            "• red / красное\n"
            "• black / чёрное\n"
            "• even / чёт\n"
            "• odd / нечёт\n"
            "• число от 0 до 36"
        )
        return

    bet_type, bet_value = parsed

    try:
        amount = int(args[2])
    except ValueError:
        await message.reply("Сумма ставки должна быть числом.")
        return

    if amount <= 0:
        await message.reply("Ставка должна быть положительной.")
        return

    user_id = message.from_user.id
    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games
    display_name = message.from_user.username or message.from_user.full_name

    async with _bet_lock:
        async for session in get_session():
            # Проверяем наличие активного раунда
            rnd = await get_active_round(session, chat_id, topic_id)
            if rnd is None:
                await message.reply("Сейчас нет активного раунда. Ожидайте начала.")
                return

            # Проверяем лимит ставок
            bets_count = await get_user_bets_count(session, rnd.id, user_id)
            if bets_count >= MAX_BETS_PER_ROUND:
                await message.reply(f"Максимум {MAX_BETS_PER_ROUND} ставки за раунд.")
                return

            # Проверяем и списываем баланс
            stats = await deduct_coins(session, user_id, chat_id, amount)
            if stats is None:
                user_stats = await get_or_create_user_stats(session, user_id, chat_id)
                await message.reply(
                    f"Недостаточно монет. Твой баланс: {user_stats.coins} монет."
                )
                return

            # Размещаем ставку
            await place_bet(session, rnd.id, user_id, bet_type, bet_value, amount, display_name)
            await session.commit()

    bet_desc = format_bet_description(bet_type, bet_value)
    await message.reply(
        f"Ставка принята: {bet_desc} — {amount} монет.\n"
        f"Твой баланс: {stats.coins} монет."
    )


async def start_roulette_round(bot: Bot) -> None:
    """Запускает раунд рулетки (вызывается из планировщика или автоматически)."""
    global _round_task
    if settings.topic_games is None:
        return
    if not _is_roulette_time():
        return
    if _round_task and not _round_task.done():
        return  # Раунд уже идёт

    _round_task = asyncio.create_task(
        _run_roulette_round(bot, settings.forum_chat_id, settings.topic_games)
    )


async def _run_roulette_round(bot: Bot, chat_id: int, topic_id: int) -> None:
    """Полный цикл одного раунда рулетки."""
    try:
        # 1. Создаём раунд
        async for session in get_session():
            rnd = await create_round(session, chat_id, topic_id)
            round_id = rnd.id
            await session.commit()
            break

        # 2. Объявляем приём ставок
        await bot.send_message(
            chat_id,
            f"🎰 Рулетка открыта! Приём ставок — {BETTING_DURATION_SEC // 60} минуты.\n\n"
            "Как ставить:\n"
            "/bet red 50 — на красное\n"
            "/bet black 30 — на чёрное\n"
            "/bet even 20 — на чёт\n"
            "/bet odd 20 — на нечёт\n"
            "/bet 17 10 — на число\n\n"
            f"Максимум {MAX_BETS_PER_ROUND} ставки за раунд.",
            message_thread_id=topic_id,
        )

        # 3. Ждём приём ставок
        await asyncio.sleep(BETTING_DURATION_SEC)

        # 4. Закрываем ставки
        await bot.send_message(
            chat_id,
            "Ставки закрыты! Крутим рулетку... 🎰",
            message_thread_id=topic_id,
        )

        # 5. Анимация вращения (серия сообщений)
        spin_frames = ["⬜🔴⚫🔴⚫🟢⚫🔴...", "...🔴⚫🟢🔴⚫🔴⬜...", "......⚫🔴🟢⚫🔴⬜"]
        for frame in spin_frames:
            await bot.send_message(chat_id, frame, message_thread_id=topic_id)
            await asyncio.sleep(3)

        # 6. Генерируем результат
        result_number = spin_wheel()
        result_color = get_number_color(result_number)
        result_parity = get_number_parity(result_number)

        await bot.send_message(
            chat_id,
            f"🎰 Выпало: {result_number} {color_emoji(result_color)}\n"
            f"Цвет: {color_name_ru(result_color)}\n"
            f"{'Чётность: ' + parity_name_ru(result_parity) if result_parity else 'Зеро!'}",
            message_thread_id=topic_id,
        )

        # 7. Обрабатываем результаты
        async for session in get_session():
            rnd = await get_active_round(session, chat_id, topic_id)
            if rnd is None:
                return
            await close_round(session, rnd, result_number)

            bets = await get_round_bets(session, rnd.id)
            if not bets:
                await session.commit()
                await bot.send_message(
                    chat_id,
                    "В этом раунде никто не ставил.",
                    message_thread_id=topic_id,
                )
            else:
                winners_lines: list[str] = []
                losers_lines: list[str] = []

                # Группируем ставки по пользователю для статистики
                user_results: dict[int, tuple[str | None, int, int]] = {}  # user_id: (name, won, lost)

                for bet in bets:
                    winnings = calculate_winnings(bet.bet_type, bet.bet_value, bet.amount, result_number)
                    name = bet.display_name or str(bet.user_id)
                    bet_desc = format_bet_description(bet.bet_type, bet.bet_value)

                    prev = user_results.get(bet.user_id, (name, 0, 0))
                    if winnings > 0:
                        await credit_coins(session, bet.user_id, chat_id, winnings, display_name=name)
                        user_results[bet.user_id] = (name, prev[1] + winnings, prev[2])
                        winners_lines.append(f"• @{name}: {bet_desc} ({bet.amount}) → +{winnings} монет")
                    else:
                        user_results[bet.user_id] = (name, prev[1], prev[2] + bet.amount)
                        losers_lines.append(f"• @{name}: {bet_desc} ({bet.amount}) — проигрыш")

                # Обновляем статистику рулетки
                for uid, (uname, won, lost) in user_results.items():
                    await update_roulette_stats(session, uid, chat_id, won, lost, display_name=uname)

                await session.commit()

                # Публикуем результаты
                result_lines = ["Результаты раунда:"]
                if winners_lines:
                    result_lines.append("\n🏆 Победители:")
                    result_lines.extend(winners_lines)
                if losers_lines:
                    result_lines.append("\n❌ Проигрыши:")
                    result_lines.extend(losers_lines)

                # Показываем обновлённые балансы
                result_lines.append("\n💰 Балансы:")
                for uid, (uname, _, _) in user_results.items():
                    stats = await get_or_create_user_stats(session, uid, chat_id)
                    result_lines.append(f"• @{uname}: {stats.coins} монет")

                await bot.send_message(
                    chat_id,
                    "\n".join(result_lines),
                    message_thread_id=topic_id,
                )
            break

        # 8. Если время ещё позволяет — запускаем новый раунд
        await asyncio.sleep(5)
        if _is_roulette_time():
            await _run_roulette_round(bot, chat_id, topic_id)

    except asyncio.CancelledError:
        logger.info("Раунд рулетки отменён.")
    except Exception:
        logger.exception("Ошибка в раунде рулетки.")
