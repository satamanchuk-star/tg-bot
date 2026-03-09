"""Почему: логика взаимодействия с пользователем в рулетке изолирована в отдельном роутере."""

from __future__ import annotations

import asyncio
import logging
from itertools import cycle

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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

# Префикс для callback-данных рулетки
_CB = "rlt"
# Быстрые суммы ставок
_QUICK_AMOUNTS = [10, 25, 50, 100]

_ROULETTE_MAIN_CHAT_INVITES = cycle(
    [
        "Через 5 минут в «Блэкджек и боулинг» открывается рулетка! Крутите колесо фортуны 🎰",
        "Соседи, рулетка стартует через 5 минут! Заходите в игровой топик испытать удачу 🍀",
        "Кто сегодня сорвёт куш? Рулетка через 5 минут в «Блэкджек и боулинг»! 💰",
        "Внимание! Через 5 минут начинается рулетка. Готовьте ставки и нервы 😎",
        "Колесо фортуны ждёт! Рулетка через 5 минут в игровом топике 🎡",
    ]
)

_ROULETTE_TOPIC_INVITES = cycle(
    [
        "Через 5 минут рулетка! Красное или чёрное — делайте ваши ставки 🔴⚫",
        "Рулетка через 5 минут! Кто поставит на зеро и станет легендой? 🟢",
        "5 минут до рулетки! Подготовьте монеты и интуицию 🎲",
        "Скоро крутим колесо! Рулетка стартует через 5 минут — не пропустите 🎰",
        "Через 5 минут начинаем! Чёт или нечёт? Решать вам 🤔",
    ]
)

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


def _is_in_game_topic_cb(callback: CallbackQuery) -> bool:
    if settings.topic_games is None or callback.message is None:
        return False
    return (
        callback.message.chat.id == settings.forum_chat_id
        and callback.message.message_thread_id == settings.topic_games
    )


# --- Inline-клавиатуры для быстрых ставок ---

def _bet_type_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора типа ставки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 Красное", callback_data=f"{_CB}:type:color:red"),
            InlineKeyboardButton(text="⚫ Чёрное", callback_data=f"{_CB}:type:color:black"),
        ],
        [
            InlineKeyboardButton(text="Чёт", callback_data=f"{_CB}:type:parity:even"),
            InlineKeyboardButton(text="Нечёт", callback_data=f"{_CB}:type:parity:odd"),
        ],
        [
            InlineKeyboardButton(text="🟢 Зеро (0)", callback_data=f"{_CB}:type:number:0"),
        ],
    ])


def _amount_keyboard(bet_type: str, bet_value: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора суммы ставки."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for amount in _QUICK_AMOUNTS:
        row.append(
            InlineKeyboardButton(
                text=f"{amount} монет",
                callback_data=f"{_CB}:bet:{bet_type}:{bet_value}:{amount}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data=f"{_CB}:back"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- Команда /roulette для быстрого выбора ставки ---

@router.message(Command("roulette"))
async def handle_roulette_menu(message: Message) -> None:
    """Показывает inline-меню для быстрой ставки."""
    if not _is_in_game_topic(message):
        return
    if message.from_user is None:
        return

    if not _is_roulette_time():
        await message.reply("Рулетка работает с 21:00 до 22:00 по Москве.")
        return

    async for session in get_session():
        rnd = await get_active_round(session, settings.forum_chat_id, settings.topic_games)
        if rnd is None:
            await message.reply("Сейчас нет активного раунда. Ожидайте начала.")
            return
        user_stats = await get_or_create_user_stats(session, message.from_user.id, settings.forum_chat_id)
        balance = user_stats.coins
        break

    await message.reply(
        f"🎰 Выберите тип ставки\n💰 Ваш баланс: {balance} монет",
        reply_markup=_bet_type_keyboard(),
    )


@router.callback_query(F.data == f"{_CB}:back")
async def roulette_back(callback: CallbackQuery) -> None:
    """Возврат к выбору типа ставки."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    async for session in get_session():
        user_stats = await get_or_create_user_stats(session, callback.from_user.id, settings.forum_chat_id)
        balance = user_stats.coins
        break

    await callback.message.edit_text(
        f"🎰 Выберите тип ставки\n💰 Ваш баланс: {balance} монет",
        reply_markup=_bet_type_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{_CB}:type:"))
async def roulette_select_type(callback: CallbackQuery) -> None:
    """Обработка выбора типа ставки — показываем выбор суммы."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    bet_type = parts[2]
    bet_value = parts[3]
    bet_desc = format_bet_description(bet_type, bet_value)

    async for session in get_session():
        user_stats = await get_or_create_user_stats(session, callback.from_user.id, settings.forum_chat_id)
        balance = user_stats.coins
        break

    await callback.message.edit_text(
        f"🎰 Ставка: {bet_desc}\n💰 Баланс: {balance} монет\n\nВыберите сумму:",
        reply_markup=_amount_keyboard(bet_type, bet_value),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{_CB}:bet:"))
async def roulette_place_bet(callback: CallbackQuery) -> None:
    """Обработка быстрой ставки через кнопку."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if not _is_in_game_topic_cb(callback):
        await callback.answer("Ставки принимаются только в игровом топике.")
        return
    if not _is_roulette_time():
        await callback.answer("Рулетка работает с 21:00 до 22:00 по Москве.")
        return

    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer()
        return

    bet_type = parts[2]
    bet_value = parts[3]
    try:
        amount = int(parts[4])
    except ValueError:
        await callback.answer()
        return

    user_id = callback.from_user.id
    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games
    display_name = callback.from_user.username or callback.from_user.full_name

    async with _bet_lock:
        async for session in get_session():
            rnd = await get_active_round(session, chat_id, topic_id)
            if rnd is None:
                await callback.answer("Сейчас нет активного раунда.")
                return

            bets_count = await get_user_bets_count(session, rnd.id, user_id)
            if bets_count >= MAX_BETS_PER_ROUND:
                await callback.answer(f"Максимум {MAX_BETS_PER_ROUND} ставки за раунд.")
                return

            stats = await deduct_coins(session, user_id, chat_id, amount)
            if stats is None:
                user_stats = await get_or_create_user_stats(session, user_id, chat_id)
                await callback.answer(f"Недостаточно монет. Баланс: {user_stats.coins}")
                return

            await place_bet(session, rnd.id, user_id, bet_type, bet_value, amount, display_name)
            await session.commit()

    bet_desc = format_bet_description(bet_type, bet_value)
    await callback.message.edit_text(
        f"Ставка принята: {bet_desc} — {amount} монет.\n"
        f"💰 Баланс: {stats.coins} монет.\n\n"
        "Ещё ставка? Нажмите /roulette",
    )
    await callback.answer(f"Ставка: {bet_desc} — {amount} монет")


# --- Команда /bet (текстовый вариант) ---

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
            "Пример: /bet red 50\n\n"
            "Или используйте /roulette для быстрого выбора кнопками."
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
            "• число от 0 до 36\n\n"
            "Или нажмите /roulette для быстрого выбора."
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


async def announce_roulette_soon(bot: Bot) -> None:
    """Анонс рулетки за 5 минут до старта."""
    if settings.topic_games is None:
        return
    await bot.send_message(settings.forum_chat_id, next(_ROULETTE_MAIN_CHAT_INVITES))
    await bot.send_message(
        settings.forum_chat_id,
        next(_ROULETTE_TOPIC_INVITES),
        message_thread_id=settings.topic_games,
    )


async def announce_roulette_rules(bot: Bot) -> None:
    """Публикует правила рулетки за минуту до старта."""
    if settings.topic_games is None:
        return
    await bot.send_message(
        settings.forum_chat_id,
        "📋 Правила рулетки\n\n"
        "• Ставки принимаются 2 минуты после открытия раунда\n"
        f"• Максимум {MAX_BETS_PER_ROUND} ставки за раунд\n"
        "• Типы ставок:\n"
        "  🔴 Красное / ⚫ Чёрное — выигрыш x2\n"
        "  Чёт / Нечёт — выигрыш x2\n"
        "  Число (0-36) — выигрыш x36\n"
        "  🟢 Зеро (0) — все ставки на цвет и чёт/нечёт проигрывают\n\n"
        "Как ставить:\n"
        "• /roulette — быстрый выбор кнопками\n"
        "• /bet red 50 — текстовая команда\n\n"
        "Рулетка стартует через 1 минуту. Удачи! 🍀",
        message_thread_id=settings.topic_games,
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

        # 2. Объявляем приём ставок с inline-кнопками
        await bot.send_message(
            chat_id,
            f"🎰 Рулетка открыта! Приём ставок — {BETTING_DURATION_SEC // 60} минуты.\n\n"
            "Как ставить:\n"
            "• /roulette — быстрая ставка кнопками\n"
            "• /bet red 50 — текстом\n\n"
            "Типы: красное, чёрное, чёт, нечёт, число 0-36\n"
            f"Максимум {MAX_BETS_PER_ROUND} ставки за раунд.",
            message_thread_id=topic_id,
            reply_markup=_bet_type_keyboard(),
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
                        winners_lines.append(f"• @{name}: {bet_desc} ({bet.amount}) -> +{winnings} монет")
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
