"""Хендлеры рулетки. Каждый пользователь получает персональное сообщение со ставкой,
чтобы несколько игроков могли ставить одновременно."""

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
    SPIN_DURATION_SEC,
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
    parity_name_ru,
    parse_bet,
    place_bet,
    spin_wheel,
    update_roulette_stats,
)
from app.utils.safe_telegram import safe_call
from app.utils.time import is_game_time_allowed

logger = logging.getLogger(__name__)
router = Router()

# Лок для защиты от гонок при размещении ставок
_bet_lock = asyncio.Lock()

# Быстрые суммы ставок
_QUICK_AMOUNTS = [10, 25, 50, 100, 250, 500]

_ROULETTE_MAIN_CHAT_INVITES = cycle(
    [
        "Через 5 минут в «Блэкджек и боулинг» открывается рулетка! Крутите колесо фортуны",
        "Соседи, рулетка стартует через 5 минут! Заходите в игровой топик испытать удачу",
        "Кто сегодня сорвёт куш? Рулетка через 5 минут в «Блэкджек и боулинг»!",
        "Внимание! Через 5 минут начинается рулетка. Готовьте ставки и нервы",
        "Колесо фортуны ждёт! Рулетка через 5 минут в игровом топике",
    ]
)

_ROULETTE_TOPIC_INVITES = cycle(
    [
        "Через 5 минут рулетка! Красное или чёрное — делайте ваши ставки",
        "Рулетка через 5 минут! Кто поставит на зеро и станет легендой?",
        "5 минут до рулетки! Подготовьте монеты и интуицию",
        "Скоро крутим колесо! Рулетка стартует через 5 минут — не пропустите",
        "Через 5 минут начинаем! Чёт или нечёт? Решать вам",
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


# ---------------------------------------------------------------------------
# Inline-клавиатуры
# ---------------------------------------------------------------------------

def _shared_keyboard() -> InlineKeyboardMarkup:
    """Кнопка на общем объявлении раунда."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сделать ставку", callback_data="r:go")],
    ])


def _type_keyboard(uid: int) -> InlineKeyboardMarkup:
    """Персональная клавиатура выбора типа ставки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Красное", callback_data=f"r:u:{uid}:t:color:red"),
            InlineKeyboardButton(text="Чёрное", callback_data=f"r:u:{uid}:t:color:black"),
        ],
        [
            InlineKeyboardButton(text="Чёт", callback_data=f"r:u:{uid}:t:parity:even"),
            InlineKeyboardButton(text="Нечёт", callback_data=f"r:u:{uid}:t:parity:odd"),
        ],
        [
            InlineKeyboardButton(text="Число (0-36)", callback_data=f"r:u:{uid}:nums"),
        ],
    ])


def _number_keyboard(uid: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора числа 0-36."""
    rows: list[list[InlineKeyboardButton]] = []
    # Зеро отдельно
    rows.append([InlineKeyboardButton(text="0", callback_data=f"r:u:{uid}:n:0")])
    # Числа 1-36 по 6 в ряд
    for start in range(1, 37, 6):
        row = []
        for n in range(start, min(start + 6, 37)):
            row.append(InlineKeyboardButton(text=str(n), callback_data=f"r:u:{uid}:n:{n}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"r:u:{uid}:bk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _amount_keyboard(uid: int, bet_type: str, bet_value: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора суммы ставки."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for amount in _QUICK_AMOUNTS:
        row.append(
            InlineKeyboardButton(
                text=str(amount),
                callback_data=f"r:u:{uid}:a:{bet_type}:{bet_value}:{amount}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Ва-банк
    rows.append([
        InlineKeyboardButton(text="Всё (ва-банк)", callback_data=f"r:u:{uid}:ai:{bet_type}:{bet_value}"),
    ])
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data=f"r:u:{uid}:bk"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _extract_uid(data: str, idx: int = 2) -> int | None:
    """Извлекает user_id из callback_data по индексу."""
    parts = data.split(":")
    try:
        return int(parts[idx])
    except (IndexError, ValueError):
        return None


async def _get_balance(user_id: int) -> int:
    async for session in get_session():
        stats = await get_or_create_user_stats(session, user_id, settings.forum_chat_id)
        return stats.coins
    return 0


# ---------------------------------------------------------------------------
# Команда /roulette — персональное меню
# ---------------------------------------------------------------------------

@router.message(Command("roulette"))
async def handle_roulette_menu(message: Message) -> None:
    """Показывает персональное inline-меню для ставки."""
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
        balance = (await get_or_create_user_stats(session, message.from_user.id, settings.forum_chat_id)).coins
        break

    uid = message.from_user.id
    await message.reply(
        f"Выберите тип ставки\n"
        f"Баланс: {balance} монет",
        reply_markup=_type_keyboard(uid),
    )


# ---------------------------------------------------------------------------
# Callback: кнопка «Сделать ставку» на общем объявлении
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "r:go")
async def handle_start_bet(callback: CallbackQuery) -> None:
    """Отправляет персональное сообщение для ставки (из общего объявления)."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if not _is_in_game_topic_cb(callback):
        await callback.answer("Ставки принимаются только в игровом топике.")
        return
    if not _is_roulette_time():
        await callback.answer("Рулетка работает с 21:00 до 22:00 по Москве.")
        return

    uid = callback.from_user.id
    async for session in get_session():
        rnd = await get_active_round(session, settings.forum_chat_id, settings.topic_games)
        if rnd is None:
            await callback.answer("Сейчас нет активного раунда.")
            return
        balance = (await get_or_create_user_stats(session, uid, settings.forum_chat_id)).coins
        break

    display = callback.from_user.username or callback.from_user.full_name
    # Отправляем персональное сообщение в тот же тред (НЕ редактируем общее)
    # message_thread_id копируется автоматически из callback.message в answer()
    await callback.message.answer(
        f"@{display}, выберите тип ставки\n"
        f"Баланс: {balance} монет",
        reply_markup=_type_keyboard(uid),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: выбор типа ставки (персональное сообщение)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:t:"))
async def handle_type_select(callback: CallbackQuery) -> None:
    """Пользователь выбрал тип ставки — показываем суммы."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    parts = callback.data.split(":")
    # r:u:{uid}:t:{bet_type}:{bet_value}
    if len(parts) != 6:
        await callback.answer()
        return

    bet_type = parts[4]
    bet_value = parts[5]
    bet_desc = format_bet_description(bet_type, bet_value)
    balance = await _get_balance(uid)

    await callback.message.edit_text(
        f"Ставка: {bet_desc}\n"
        f"Баланс: {balance} монет\n\n"
        "Выберите сумму (или /bet <тип> <сумма> для произвольной):",
        reply_markup=_amount_keyboard(uid, bet_type, bet_value),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: показать сетку чисел
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:nums$"))
async def handle_show_numbers(callback: CallbackQuery) -> None:
    """Показывает клавиатуру выбора числа."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    await callback.message.edit_text(
        "Выберите число (0-36):",
        reply_markup=_number_keyboard(uid),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: выбрано конкретное число — показываем суммы
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:n:\d+$"))
async def handle_number_select(callback: CallbackQuery) -> None:
    """Пользователь выбрал число — показываем суммы."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    parts = callback.data.split(":")
    number = parts[4]
    balance = await _get_balance(uid)

    await callback.message.edit_text(
        f"Ставка: число {number}\n"
        f"Баланс: {balance} монет\n\n"
        "Выберите сумму:",
        reply_markup=_amount_keyboard(uid, "number", number),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: выбрана сумма — размещаем ставку
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:a:"))
async def handle_amount_select(callback: CallbackQuery) -> None:
    """Размещает ставку с выбранной суммой."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if not _is_in_game_topic_cb(callback):
        await callback.answer("Ставки принимаются только в игровом топике.")
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    parts = callback.data.split(":")
    # r:u:{uid}:a:{bet_type}:{bet_value}:{amount}
    if len(parts) != 7:
        await callback.answer()
        return

    bet_type = parts[4]
    bet_value = parts[5]
    try:
        amount = int(parts[6])
    except ValueError:
        await callback.answer()
        return

    await _do_place_bet(callback, uid, bet_type, bet_value, amount)


# ---------------------------------------------------------------------------
# Callback: ва-банк
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:ai:"))
async def handle_all_in(callback: CallbackQuery) -> None:
    """Ставка ва-банк — все монеты."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if not _is_in_game_topic_cb(callback):
        await callback.answer("Ставки принимаются только в игровом топике.")
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    parts = callback.data.split(":")
    # r:u:{uid}:ai:{bet_type}:{bet_value}
    if len(parts) != 6:
        await callback.answer()
        return

    bet_type = parts[4]
    bet_value = parts[5]

    balance = await _get_balance(uid)
    if balance <= 0:
        await callback.answer("У вас 0 монет.")
        return

    await _do_place_bet(callback, uid, bet_type, bet_value, balance)


# ---------------------------------------------------------------------------
# Callback: кнопка «Назад»
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:bk$"))
async def handle_back(callback: CallbackQuery) -> None:
    """Возврат к выбору типа ставки."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    balance = await _get_balance(uid)
    await callback.message.edit_text(
        f"Выберите тип ставки\n"
        f"Баланс: {balance} монет",
        reply_markup=_type_keyboard(uid),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: ещё одна ставка
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^r:u:\d+:more$"))
async def handle_more(callback: CallbackQuery) -> None:
    """После размещения ставки — предложить ещё одну."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    uid = _extract_uid(callback.data)
    if uid is None or callback.from_user.id != uid:
        await callback.answer("Это не ваша ставка.")
        return

    async for session in get_session():
        rnd = await get_active_round(session, settings.forum_chat_id, settings.topic_games)
        if rnd is None:
            await callback.message.edit_text("Раунд уже завершён.")
            await callback.answer()
            return
        break

    balance = await _get_balance(uid)
    await callback.message.edit_text(
        f"Выберите тип ставки\n"
        f"Баланс: {balance} монет",
        reply_markup=_type_keyboard(uid),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Общая логика размещения ставки
# ---------------------------------------------------------------------------

async def _do_place_bet(
    callback: CallbackQuery,
    uid: int,
    bet_type: str,
    bet_value: str,
    amount: int,
) -> None:
    """Списывает монеты и размещает ставку."""
    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games
    display_name = callback.from_user.username or callback.from_user.full_name

    async with _bet_lock:
        async for session in get_session():
            rnd = await get_active_round(session, chat_id, topic_id)
            if rnd is None:
                await callback.message.edit_text("Раунд уже завершён, ставки не принимаются.")
                await callback.answer()
                return

            stats = await deduct_coins(session, uid, chat_id, amount)
            if stats is None:
                user_stats = await get_or_create_user_stats(session, uid, chat_id)
                await callback.answer(f"Недостаточно монет. Баланс: {user_stats.coins}")
                return

            await place_bet(session, rnd.id, uid, bet_type, bet_value, amount, display_name)
            await session.commit()
            new_balance = stats.coins
            break

    bet_desc = format_bet_description(bet_type, bet_value)
    await callback.message.edit_text(
        f"Ставка принята: {bet_desc} — {amount} монет\n"
        f"Баланс: {new_balance} монет",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ещё ставка", callback_data=f"r:u:{uid}:more")],
        ]),
    )
    await callback.answer(f"Ставка: {bet_desc} — {amount}")


# ---------------------------------------------------------------------------
# Команда /bet (текстовый вариант — любая сумма)
# ---------------------------------------------------------------------------

@router.message(Command("bet"))
async def handle_bet(message: Message) -> None:
    """Обработчик /bet <тип> <сумма> — позволяет ввести произвольную сумму."""
    if not _is_in_game_topic(message):
        return
    if message.from_user is None:
        return

    if not _is_roulette_time():
        await message.reply("Рулетка работает с 21:00 до 22:00 по Москве.")
        return

    args = (message.text or "").split()
    if len(args) < 3:
        await message.reply(
            "Формат: /bet <тип> <сумма>\n"
            "Типы: red/красное, black/чёрное, even/чёт, odd/нечёт, число (0-36)\n"
            "Пример: /bet red 50\n\n"
            "Или используйте /roulette для выбора кнопками."
        )
        return

    raw_type = args[1]
    parsed = parse_bet(raw_type)
    if parsed is None:
        await message.reply(
            "Неизвестный тип ставки. Доступны:\n"
            "red / красное\n"
            "black / чёрное\n"
            "even / чёт\n"
            "odd / нечёт\n"
            "число от 0 до 36\n\n"
            "Или нажмите /roulette для выбора кнопками."
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
            rnd = await get_active_round(session, chat_id, topic_id)
            if rnd is None:
                await message.reply("Сейчас нет активного раунда. Ожидайте начала.")
                return

            stats = await deduct_coins(session, user_id, chat_id, amount)
            if stats is None:
                user_stats = await get_or_create_user_stats(session, user_id, chat_id)
                await message.reply(
                    f"Недостаточно монет. Баланс: {user_stats.coins} монет."
                )
                return

            await place_bet(session, rnd.id, user_id, bet_type, bet_value, amount, display_name)
            await session.commit()
            break

    bet_desc = format_bet_description(bet_type, bet_value)
    await message.reply(
        f"Ставка принята: {bet_desc} — {amount} монет.\n"
        f"Баланс: {stats.coins} монет."
    )


# ---------------------------------------------------------------------------
# Анонсы и планировщик
# ---------------------------------------------------------------------------

async def announce_roulette_soon(bot: Bot) -> None:
    """Анонс рулетки за 5 минут до старта."""
    if settings.topic_games is None:
        return
    # Анонс в General может упасть (топик закрыт) — не ломаем анонс в игровом топике
    try:
        await bot.send_message(settings.forum_chat_id, next(_ROULETTE_MAIN_CHAT_INVITES))
    except Exception:
        logger.warning("Не удалось отправить анонс рулетки в General-топик.")
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
        "Правила рулетки\n\n"
        f"Ставки принимаются {BETTING_DURATION_SEC} секунд после открытия раунда\n"
        "Количество ставок не ограничено\n"
        "Типы ставок:\n"
        "  Красное / Чёрное — выигрыш x2\n"
        "  Чёт / Нечёт — выигрыш x2\n"
        "  Число (0-36) — выигрыш x36\n"
        "  Зеро (0) — все ставки на цвет и чёт/нечёт проигрывают\n\n"
        "Как ставить:\n"
        "  /roulette — быстрый выбор кнопками\n"
        "  /bet red 50 — текстовая команда (любая сумма)\n\n"
        "Рулетка стартует через 1 минуту. Удачи!",
        message_thread_id=settings.topic_games,
    )


async def start_roulette_round(bot: Bot) -> None:
    """Запускает раунд рулетки (вызывается из планировщика)."""
    global _round_task
    if settings.topic_games is None:
        return
    if not _is_roulette_time():
        return
    if _round_task and not _round_task.done():
        return

    _round_task = asyncio.create_task(
        _run_roulette_round(bot, settings.forum_chat_id, settings.topic_games)
    )


async def resume_roulette_if_needed(bot: Bot) -> None:
    """Возобновляет рулетку после перезагрузки бота, если сейчас время игры (21-22)."""
    if not _is_roulette_time():
        return
    if settings.topic_games is None:
        return
    # Закрываем зависший раунд (если бот упал посреди раунда)
    async for session in get_session():
        stale = await get_active_round(session, settings.forum_chat_id, settings.topic_games)
        if stale is not None:
            logger.info("Рулетка: закрываем зависший раунд #%s после перезагрузки.", stale.id)
            await close_round(session, stale, -1)  # -1 = раунд отменён
            await session.commit()
        break
    # Проверяем доступность Telegram перед запуском нового раунда
    try:
        await asyncio.wait_for(bot.get_me(), timeout=5)
    except Exception:
        logger.warning("Рулетка: Telegram недоступен, откладываем возобновление.")
        return
    logger.info("Рулетка: время игры (21-22), возобновляем раунды после перезагрузки.")
    await start_roulette_round(bot)


async def _run_roulette_round(bot: Bot, chat_id: int, topic_id: int) -> None:
    """Полный цикл раундов рулетки (крутится пока _is_roulette_time())."""
    while True:
        round_id: int | None = None
        try:
            # 1. Создаём раунд
            async for session in get_session():
                rnd = await create_round(session, chat_id, topic_id)
                round_id = rnd.id
                await session.commit()
                break

            # 2. Объявляем приём ставок — общее сообщение с кнопкой
            await bot.send_message(
                chat_id,
                f"Рулетка открыта! Приём ставок — {BETTING_DURATION_SEC} секунд.\n\n"
                "Как ставить:\n"
                "  Нажмите кнопку ниже\n"
                "  /roulette — меню кнопками\n"
                "  /bet <тип> <сумма> — текстом (любая сумма)\n\n"
                "Типы: красное, чёрное, чёт, нечёт, число 0-36\n"
                "Количество ставок не ограничено!",
                message_thread_id=topic_id,
                reply_markup=_shared_keyboard(),
            )

            # 3. Ждём приём ставок
            await asyncio.sleep(BETTING_DURATION_SEC)

            # 4. Закрываем ставки
            spin_msg = await bot.send_message(
                chat_id,
                "Ставки закрыты! Крутим рулетку...",
                message_thread_id=topic_id,
            )

            # 5. Анимация вращения (10 секунд)
            spin_frames = [
                "Крутим рулетку...\n⬜🔴⚫🔴⚫🟢⚫🔴",
                "Крутим рулетку...\n🔴⚫🟢🔴⚫🔴⬜⚫",
                "Крутим рулетку...\n⚫🔴⬜🟢⚫🔴⚫🔴",
                "Крутим рулетку...\n🟢⚫🔴⚫🔴⬜⚫🔴",
            ]
            frame_delay = SPIN_DURATION_SEC / len(spin_frames)
            for frame in spin_frames:
                await asyncio.sleep(frame_delay)
                try:
                    await spin_msg.edit_text(frame)
                except Exception:
                    pass

            # 6. Генерируем результат
            result_number = spin_wheel()
            result_color = get_number_color(result_number)
            result_parity = get_number_parity(result_number)

            parity_text = f"Чётность: {parity_name_ru(result_parity)}" if result_parity else "Зеро!"
            await safe_call(
                bot.send_message(
                    chat_id,
                    f"Выпало: {result_number} {color_emoji(result_color)}\n"
                    f"Цвет: {color_name_ru(result_color)}\n"
                    f"{parity_text}",
                    message_thread_id=topic_id,
                ),
                log_ctx="roulette_result",
            )

            # 7. Обрабатываем результаты
            async for session in get_session():
                rnd = await get_active_round(session, chat_id, topic_id)
                if rnd is None:
                    break
                await close_round(session, rnd, result_number)

                bets = await get_round_bets(session, rnd.id)
                if not bets:
                    await session.commit()
                    await safe_call(
                        bot.send_message(
                            chat_id,
                            "В этом раунде никто не ставил.",
                            message_thread_id=topic_id,
                        ),
                        log_ctx="roulette_no_bets",
                    )
                else:
                    winners_lines: list[str] = []
                    losers_lines: list[str] = []

                    # Группируем ставки по пользователю для статистики
                    user_results: dict[int, tuple[str | None, int, int]] = {}

                    for bet in bets:
                        winnings = calculate_winnings(bet.bet_type, bet.bet_value, bet.amount, result_number)
                        name = bet.display_name or str(bet.user_id)
                        bet_desc = format_bet_description(bet.bet_type, bet.bet_value)

                        prev = user_results.get(bet.user_id, (name, 0, 0))
                        if winnings > 0:
                            await credit_coins(session, bet.user_id, chat_id, winnings, display_name=name)
                            user_results[bet.user_id] = (name, prev[1] + winnings, prev[2])
                            winners_lines.append(f"  @{name}: {bet_desc} ({bet.amount}) -> +{winnings} монет")
                        else:
                            user_results[bet.user_id] = (name, prev[1], prev[2] + bet.amount)
                            losers_lines.append(f"  @{name}: {bet_desc} ({bet.amount}) — проигрыш")

                    # Обновляем статистику
                    for uid_key, (uname, won, lost) in user_results.items():
                        await update_roulette_stats(session, uid_key, chat_id, won, lost, display_name=uname)

                    await session.commit()

                    # Публикуем результаты
                    result_lines = ["Результаты раунда:"]
                    if winners_lines:
                        result_lines.append("\nПобедители:")
                        result_lines.extend(winners_lines)
                    if losers_lines:
                        result_lines.append("\nПроигрыши:")
                        result_lines.extend(losers_lines)

                    result_lines.append("\nБалансы:")
                    for uid_key, (uname, _, _) in user_results.items():
                        st = await get_or_create_user_stats(session, uid_key, chat_id)
                        result_lines.append(f"  @{uname}: {st.coins} монет")

                    await safe_call(
                        bot.send_message(
                            chat_id,
                            "\n".join(result_lines),
                            message_thread_id=topic_id,
                        ),
                        log_ctx="roulette_results",
                    )
                break

        except asyncio.CancelledError:
            logger.info("Раунд рулетки отменён.")
            return
        except Exception:
            logger.exception("Ошибка в раунде рулетки.")
            # Закрываем зависший раунд и возвращаем ставки
            if round_id is not None:
                try:
                    async for session in get_session():
                        rnd = await get_active_round(session, chat_id, topic_id)
                        if rnd is not None:
                            bets = await get_round_bets(session, rnd.id)
                            for bet in bets:
                                await credit_coins(
                                    session, bet.user_id, chat_id, bet.amount,
                                    display_name=bet.display_name,
                                )
                            await close_round(session, rnd, -1)
                            await session.commit()
                            logger.info(
                                "Раунд #%s закрыт аварийно, ставки возвращены (%d).",
                                round_id, len(bets),
                            )
                        break
                except Exception:
                    logger.exception("Не удалось закрыть зависший раунд #%s.", round_id)

        # Пауза между раундами
        await asyncio.sleep(5)
        if not _is_roulette_time():
            break
