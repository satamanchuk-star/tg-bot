"""Почему: анкеты (шлагбаум и соседи) требуют FSM и не должны смешиваться с модерацией."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message

from app.config import settings
from app.utils.admin import is_admin

logger = logging.getLogger(__name__)
router = Router()


class GateForm(StatesGroup):
    waiting_response = State()  # Единственное состояние для ответа на все вопросы


class NeighborForm(StatesGroup):
    name = State()
    building = State()
    about = State()


NEIGHBOR_TIMEOUT_MIN = 30
NEIGHBOR_QUESTIONS = [
    "Как тебя зовут?",
    "В каком корпусе/доме живешь?",
    "Чем увлекаешься или чем можешь быть полезен соседям?",
]
_neighbor_timeout_tasks: dict[tuple[int, int], asyncio.Task] = {}


def _format_neighbor_questions() -> str:
    lines = ["Вопросы для знакомства:"]
    for i, question in enumerate(NEIGHBOR_QUESTIONS, 1):
        lines.append(f"{i}) {question}")
    return "\n".join(lines)


def _cancel_neighbor_timeout(chat_id: int, user_id: int) -> None:
    key = (chat_id, user_id)
    task = _neighbor_timeout_tasks.pop(key, None)
    if task:
        task.cancel()


def _start_neighbor_timeout(
    bot: Bot,
    storage: object,
    chat_id: int,
    thread_id: int | None,
    user_id: int,
) -> None:
    _cancel_neighbor_timeout(chat_id, user_id)
    task = asyncio.create_task(
        _handle_neighbor_timeout(bot, storage, chat_id, thread_id, user_id)
    )
    _neighbor_timeout_tasks[(chat_id, user_id)] = task


async def _handle_neighbor_timeout(
    bot: Bot,
    storage: object,
    chat_id: int,
    thread_id: int | None,
    user_id: int,
) -> None:
    try:
        await asyncio.sleep(NEIGHBOR_TIMEOUT_MIN * 60)
        ctx = FSMContext(
            storage=storage,
            key=StorageKey(
                bot_id=bot.id,
                chat_id=chat_id,
                user_id=user_id,
            ),
        )
        current_state = await ctx.get_state()
        if not current_state or not current_state.startswith(NeighborForm.__name__):
            return
        await ctx.clear()
        await bot.send_message(
            chat_id,
            "Увы не дождался ответа, будет время расскажи о себе.\n"
            f"{_format_neighbor_questions()}",
            message_thread_id=thread_id,
        )
    finally:
        _neighbor_timeout_tasks.pop((chat_id, user_id), None)


@router.message(Command("form"))
async def start_gate_form_command(message: Message, state: FSMContext, bot: Bot) -> None:
    """Команда /form для запуска формы шлагбаума (только админы)."""
    # Только в топике TOPIC_GATE
    if message.message_thread_id != settings.topic_gate:
        await message.reply("Команда /form работает только в топике 'Шлагбаум'.")
        return

    # Только админы
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        await message.reply("Команда только для администраторов.")
        return

    # Извлечение целевого пользователя из reply
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Используй /form как ответ на сообщение пользователя.")
        return

    target = message.reply_to_message.from_user
    # Устанавливаем FSM-состояние для целевого пользователя, а не для админа
    target_state = FSMContext(
        storage=state.storage,
        key=StorageKey(
            bot_id=bot.id,
            chat_id=message.chat.id,
            user_id=target.id
        )
    )
    await target_state.set_state(GateForm.waiting_response)
    target_name = f"@{target.username}" if target.username else target.full_name
    await message.reply(
        f"{target_name}, заполни анкету:\n"
        "1) Дата и время заезда\n"
        "2) Номер автомобиля\n"
        "3) Цвет и марка машины\n"
        "4) Номер был в постоянной базе пропусков? (да/нет)\n"
        "5) Вы выезжали из ЖК или заезжали?"
    )
    logger.info(f"HANDLER: start_gate_form_command, target={target.id}")


# FSM handlers MUST be registered BEFORE catch-all handlers

@router.message(GateForm.waiting_response)
async def gate_response(message: Message, state: FSMContext, bot: Bot) -> None:
    """Обработчик ответа пользователя на все вопросы формы шлагбаума."""
    logger.info(f"HANDLER: gate_response, text={message.text!r}")
    await state.clear()

    user = message.from_user
    username_part = f" @{user.username}" if user.username else ""
    text = (
        "#проблема_шлагбаум\n"
        "Ответ пользователя:\n"
        f"{message.text}\n\n"
        f"От пользователя ({user.full_name}{username_part} {user.id})"
    )
    await bot.send_message(settings.admin_log_chat_id, text)
    await message.reply("Спасибо! Заявка отправлена администраторам.")
    logger.info("OUT: Спасибо! Заявка отправлена администраторам.")


@router.message(NeighborForm.name)
async def neighbor_name(message: Message, state: FSMContext, bot: Bot) -> None:
    logger.info(f"HANDLER: neighbor_name, text={message.text!r}")
    await state.update_data(name=message.text)
    await state.set_state(NeighborForm.building)
    await message.reply("В каком корпусе/доме живешь?")
    _start_neighbor_timeout(
        bot,
        state.storage,
        message.chat.id,
        message.message_thread_id,
        message.from_user.id,
    )
    logger.info("OUT: В каком корпусе/доме живешь?")


@router.message(NeighborForm.building)
async def neighbor_building(message: Message, state: FSMContext, bot: Bot) -> None:
    logger.info(f"HANDLER: neighbor_building, text={message.text!r}")
    await state.update_data(building=message.text)
    await state.set_state(NeighborForm.about)
    await message.reply("Чем увлекаешься или чем можешь быть полезен соседям?")
    _start_neighbor_timeout(
        bot,
        state.storage,
        message.chat.id,
        message.message_thread_id,
        message.from_user.id,
    )
    logger.info("OUT: Чем увлекаешься...")


@router.message(NeighborForm.about)
async def neighbor_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    logger.info(f"HANDLER: neighbor_finish, text={message.text!r}")
    await state.update_data(about=message.text)
    data = await state.get_data()
    await state.clear()
    _cancel_neighbor_timeout(message.chat.id, message.from_user.id)

    welcome = (
        "Приветствуем нового соседа!\n"
        f"Имя: {data.get('name')}\n"
        f"Дом/корпус: {data.get('building')}\n"
        f"О себе: {data.get('about')}"
    )
    await message.answer(welcome)
    logger.info("OUT: Приветствуем нового соседа!")


# Catch-all trigger handlers AFTER state handlers
# Используем фильтры чтобы не блокировать модерацию для других топиков

@router.message(
    F.chat.id == settings.forum_chat_id,
    F.message_thread_id == settings.topic_neighbors,
    F.from_user,
    F.text,
    StateFilter(None),
)
async def neighbor_trigger(message: Message, state: FSMContext, bot: Bot) -> None:
    logger.info(f"HANDLER: neighbor_trigger MATCH, text={message.text!r}")
    await state.set_state(NeighborForm.name)
    await message.reply("Добро пожаловать! Давай познакомимся. Как тебя зовут?")
    _start_neighbor_timeout(
        bot,
        state.storage,
        message.chat.id,
        message.message_thread_id,
        message.from_user.id,
    )
    logger.info("OUT: Добро пожаловать! Давай познакомимся...")
