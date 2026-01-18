"""Почему: анкеты (шлагбаум и соседи) требуют FSM и не должны смешиваться с модерацией."""

from __future__ import annotations

import re

from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.config import settings

router = Router()

GATE_TRIGGERS = re.compile(
    r"проблема|не\s*смог\s*проехать|закрыт\s*шлагбаум|не\s*работает\s*шлагбаум|шлагбаум\s*не\s*работает",
    re.IGNORECASE,
)


class GateForm(StatesGroup):
    arrival_time = State()
    car_number = State()
    in_pass_base = State()


class NeighborForm(StatesGroup):
    name = State()
    building = State()
    about = State()


@router.message()
async def gate_trigger(message: Message, state: FSMContext) -> None:
    if message.chat.id != settings.forum_chat_id:
        return
    if message.message_thread_id != settings.topic_gate:
        return
    if message.text is None:
        return
    if await state.get_state() is not None:
        return
    if not GATE_TRIGGERS.search(message.text):
        return
    await state.set_state(GateForm.arrival_time)
    await message.reply(
        "Похоже, проблема со шлагбаумом. Заполни анкету:\n1) Дата и время заезда?"
    )


@router.message(GateForm.arrival_time)
async def gate_arrival(message: Message, state: FSMContext) -> None:
    await state.update_data(arrival_time=message.text)
    await state.set_state(GateForm.car_number)
    await message.reply("2) Номер автомобиля?")


@router.message(GateForm.car_number)
async def gate_car(message: Message, state: FSMContext) -> None:
    await state.update_data(car_number=message.text)
    await state.set_state(GateForm.in_pass_base)
    await message.reply("3) Номер был в постоянной базе пропусков? (да/нет)")


@router.message(GateForm.in_pass_base)
async def gate_finish(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(in_pass_base=message.text)
    data = await state.get_data()
    await state.clear()

    text = (
        "#проблема_шлагбаум\n"
        f"Дата/время заезда: {data.get('arrival_time')}\n"
        f"Авто: {data.get('car_number')}\n"
        f"В базе пропусков: {data.get('in_pass_base')}\n"
        f"От пользователя: {message.from_user.full_name} ({message.from_user.id})"
    )
    await bot.send_message(settings.admin_log_chat_id, text)
    await message.reply("Спасибо! Заявка отправлена администраторам.")


@router.message()
async def neighbor_trigger(message: Message, state: FSMContext) -> None:
    if message.chat.id != settings.forum_chat_id:
        return
    if message.message_thread_id != settings.topic_neighbors:
        return
    if message.from_user is None:
        return
    if message.text is None:
        return
    if await state.get_state() is not None:
        return
    await state.set_state(NeighborForm.name)
    await message.reply("Добро пожаловать! Давай познакомимся. Как тебя зовут?")


@router.message(NeighborForm.name)
async def neighbor_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text)
    await state.set_state(NeighborForm.building)
    await message.reply("В каком корпусе/доме живешь?")


@router.message(NeighborForm.building)
async def neighbor_building(message: Message, state: FSMContext) -> None:
    await state.update_data(building=message.text)
    await state.set_state(NeighborForm.about)
    await message.reply("Чем увлекаешься или чем можешь быть полезен соседям?")


@router.message(NeighborForm.about)
async def neighbor_finish(message: Message, state: FSMContext) -> None:
    await state.update_data(about=message.text)
    data = await state.get_data()
    await state.clear()

    welcome = (
        "Приветствуем нового соседа!\n"
        f"Имя: {data.get('name')}\n"
        f"Дом/корпус: {data.get('building')}\n"
        f"О себе: {data.get('about')}"
    )
    await message.answer(welcome)
