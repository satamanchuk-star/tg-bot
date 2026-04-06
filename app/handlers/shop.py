"""Почему: магазин монет — отдельный модуль с FSM для многошаговых покупок."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import get_session
from app.services.games import get_or_create_stats
from app.services.shop import SHOP_CATALOG, get_item, record_purchase

router = Router()
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# FSM-состояния для товаров, требующих ввода
# ──────────────────────────────────────────────────────────
class ShopForm(StatesGroup):
    waiting_poll_question = State()


# ──────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────
def _is_shop_topic(message: Message) -> bool:
    """Проверяет, находится ли сообщение в допустимом топике магазина."""
    if message.chat.id != settings.forum_chat_id:
        return False
    thread = message.message_thread_id
    return thread in (settings.topic_smoke, settings.topic_games)


def _shop_keyboard() -> InlineKeyboardMarkup:
    """Генерирует клавиатуру магазина из каталога."""
    buttons = []
    for item in SHOP_CATALOG:
        buttons.append([
            InlineKeyboardButton(
                text=f"{item.name} — {item.price} монет",
                callback_data=f"shop_buy:{item.key}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ──────────────────────────────────────────────────────────
# КОМАНДА МАГАЗИН
# ──────────────────────────────────────────────────────────
@router.message(Command("магазин"))
@router.message(F.text & F.text.casefold() == "магазин")
async def shop_command(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return

    if not _is_shop_topic(message):
        topics = []
        if settings.topic_games is not None:
            topics.append("Блэкджек и боулинг (игры)")
        if settings.topic_smoke is not None:
            topics.append("Курилка")
        where = " или ".join(topics) if topics else "игровом топике"
        await message.reply(f"Магазин доступен только в топике: {where}")
        return

    await state.clear()
    await message.reply(
        "🏪 Магазин монет\n\nВыберите услугу:",
        reply_markup=_shop_keyboard(),
    )


# ──────────────────────────────────────────────────────────
# КОЛЛБЭК ПОКУПКИ
# ──────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("shop_buy:"))
async def shop_buy_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if callback.from_user is None or callback.message is None:
        await callback.answer("Ошибка.", show_alert=True)
        return

    item_key = callback.data.split(":", 1)[1]
    item = get_item(item_key)
    if item is None:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    async for session in get_session():
        stats = await get_or_create_stats(
            session,
            callback.from_user.id,
            settings.forum_chat_id,
            display_name=callback.from_user.full_name,
        )
        if stats.coins < item.price:
            await callback.answer(
                f"Недостаточно монет. Нужно {item.price}, у вас {stats.coins}.",
                show_alert=True,
            )
            return

    if item.needs_input and item.key == "poll":
        await state.set_state(ShopForm.waiting_poll_question)
        await state.update_data(item_key=item.key)
        await callback.message.reply(
            "По какому вопросу будет голосование? Напишите ваш вопрос:"
        )
        await callback.answer()
        return

    # Для товаров без ввода — мгновенная покупка
    async for session in get_session():
        stats = await get_or_create_stats(
            session,
            callback.from_user.id,
            settings.forum_chat_id,
            display_name=callback.from_user.full_name,
        )
        if stats.coins < item.price:
            await callback.answer("Недостаточно монет.", show_alert=True)
            return
        stats.coins -= item.price
        await record_purchase(
            session,
            callback.from_user.id,
            settings.forum_chat_id,
            callback.from_user.full_name,
            item.key,
            item.price,
        )
        await session.commit()
    await callback.message.reply(f"Покупка совершена: {item.name}!")
    await callback.answer()


# ──────────────────────────────────────────────────────────
# FSM: ОЖИДАНИЕ ВОПРОСА ГОЛОСОВАНИЯ
# ──────────────────────────────────────────────────────────
@router.message(ShopForm.waiting_poll_question, F.text)
async def shop_poll_question_received(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user is None:
        return

    data = await state.get_data()
    item_key = data.get("item_key", "poll")
    item = get_item(item_key)
    if item is None:
        await state.clear()
        await message.reply("Ошибка: товар не найден.")
        return

    question_text = message.text
    user_name = message.from_user.full_name
    user_id = message.from_user.id

    async for session in get_session():
        stats = await get_or_create_stats(
            session,
            user_id,
            settings.forum_chat_id,
            display_name=user_name,
        )
        if stats.coins < item.price:
            await state.clear()
            await message.reply(
                f"Недостаточно монет. Нужно {item.price}, у вас {stats.coins}."
            )
            return
        stats.coins -= item.price
        await record_purchase(
            session,
            user_id,
            settings.forum_chat_id,
            user_name,
            item.key,
            item.price,
            details={"question": question_text},
        )
        await session.commit()

    await state.clear()

    deadline = datetime.now(timezone.utc) + timedelta(days=3)
    deadline_str = deadline.strftime("%d.%m.%Y")

    await message.reply(
        f"✅ Покупка совершена!\n\n"
        f"Голосование по вашему вопросу будет организовано в течение 3 дней "
        f"(до {deadline_str}). Следите за обновлениями!"
    )

    # Уведомление в чат логов
    if settings.admin_log_chat_id:
        log_text = (
            f"🛒 Покупка в магазине\n\n"
            f"Покупатель: {user_name} (ID: {user_id})\n"
            f"Товар: {item.name}\n"
            f"Стоимость: {item.price} монет\n\n"
            f"Вопрос голосования:\n{question_text}\n\n"
            f"Срок проведения: до {deadline_str}"
        )
        try:
            await bot.send_message(settings.admin_log_chat_id, log_text)
        except Exception:
            logger.exception("Не удалось отправить уведомление о покупке в лог-чат")
