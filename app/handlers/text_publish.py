"""Почему: команда /text безопасно публикует подготовленный текст в выбранный топик форума."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings

router = Router()


class TextPublishForm(StatesGroup):
    waiting_text = State()
    waiting_topic = State()
    waiting_confirm = State()


@dataclass(frozen=True)
class TopicOption:
    title: str
    topic_id: int | None


_TOPIC_TITLES: dict[str, str] = {
    "topic_rules": "Правила",
    "topic_important": "Важно",
    "topic_buildings_41_42": "Корпуса 41/42",
    "topic_building_2": "Корпус 2",
    "topic_building_3": "Корпус 3",
    "topic_complaints": "Жалобы",
    "topic_rides": "Попутки",
    "topic_smoke": "Курение",
    "topic_pets": "Питомцы",
    "topic_repair": "Ремонт",
    "topic_realty": "Недвижимость",
    "topic_parents": "Родители",
    "topic_ads": "Объявления",
    "topic_games": "Игры",
    "topic_gate": "Шлагбаум",
    "topic_services": "Услуги",
    "topic_uk": "УК",
    "topic_neighbors": "Соседи",
    "topic_market": "Маркет",
    "topic_duplex": "Дуплексы",
}


def _topic_options() -> list[TopicOption]:
    options = [TopicOption(title="Главный чат", topic_id=None)]
    for field_name, title in _TOPIC_TITLES.items():
        topic_id = getattr(settings, field_name)
        if topic_id is None:
            continue
        options.append(TopicOption(title=title, topic_id=topic_id))
    return options


def _topic_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for option in _topic_options():
        topic_raw = "main" if option.topic_id is None else str(option.topic_id)
        rows.append(
            [
                InlineKeyboardButton(
                    text=option.title,
                    callback_data=f"txt:topic:{user_id}:{topic_raw}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data=f"txt:cancel:{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да", callback_data=f"txt:confirm:{user_id}:yes"
                ),
                InlineKeyboardButton(
                    text="Нет", callback_data=f"txt:confirm:{user_id}:no"
                ),
            ]
        ]
    )


def _is_log_chat_callback(callback: CallbackQuery) -> bool:
    return bool(callback.message and callback.message.chat.id == settings.admin_log_chat_id)


@router.message(Command("text"))
async def start_text_publish(message: Message, state: FSMContext) -> None:
    if message.chat.id != settings.admin_log_chat_id:
        return
    if message.from_user is None:
        return

    await state.clear()
    await state.set_state(TextPublishForm.waiting_text)
    await message.reply("Напишите текст который вы хотите отправить от лица бота.")


@router.message(TextPublishForm.waiting_text)
async def collect_text(message: Message, state: FSMContext) -> None:
    if message.chat.id != settings.admin_log_chat_id:
        return
    if message.from_user is None:
        return
    if not message.text:
        await message.reply("Нужен текст сообщением, попробуйте ещё раз.")
        return

    await state.update_data(draft_text=message.text)
    await state.set_state(TextPublishForm.waiting_topic)
    await message.reply(
        "В какой топик отправить сообщение?",
        reply_markup=_topic_keyboard(message.from_user.id),
    )


@router.callback_query(F.data.startswith("txt:cancel:"))
async def cancel_text_publish(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not _is_log_chat_callback(callback):
        return

    expected_user_id = callback.data.rsplit(":", maxsplit=1)[-1]
    if expected_user_id != str(callback.from_user.id):
        await callback.answer("Эту форму запустил другой пользователь.", show_alert=True)
        return

    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text("Отправка отменена.")


@router.callback_query(F.data.startswith("txt:topic:"))
async def choose_topic(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not _is_log_chat_callback(callback):
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    _, _, user_id_raw, topic_raw = parts
    if user_id_raw != str(callback.from_user.id):
        await callback.answer("Эту форму запустил другой пользователь.", show_alert=True)
        return

    if topic_raw == "main":
        selected_topic = None
    else:
        try:
            selected_topic = int(topic_raw)
        except ValueError:
            await callback.answer("Некорректный топик", show_alert=True)
            return

    option = next((item for item in _topic_options() if item.topic_id == selected_topic), None)
    if option is None:
        await callback.answer("Топик не найден", show_alert=True)
        return

    await state.update_data(topic_id=selected_topic, topic_title=option.title)
    await state.set_state(TextPublishForm.waiting_confirm)

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Вы выбрали: {option.title}.\nПодтверждаете отправку текста?",
            reply_markup=_confirm_keyboard(callback.from_user.id),
        )


@router.callback_query(F.data.startswith("txt:confirm:"))
async def confirm_send(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if callback.from_user is None or not _is_log_chat_callback(callback):
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректный ответ", show_alert=True)
        return

    _, _, user_id_raw, decision = parts
    if user_id_raw != str(callback.from_user.id):
        await callback.answer("Эту форму запустил другой пользователь.", show_alert=True)
        return

    if decision == "no":
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.edit_text("Задача завершена без отправки.")
        return

    if decision != "yes":
        await callback.answer("Некорректный ответ", show_alert=True)
        return

    data = await state.get_data()
    draft_text = data.get("draft_text")
    topic_id = data.get("topic_id")
    topic_title = data.get("topic_title")

    if not isinstance(draft_text, str) or not draft_text.strip():
        await state.clear()
        await callback.answer("Не найден текст", show_alert=True)
        if callback.message:
            await callback.message.edit_text(
                "Ошибка: текст не найден. Запустите /text заново."
            )
        return

    await bot.send_message(
        chat_id=settings.forum_chat_id,
        text=draft_text,
        message_thread_id=topic_id,
    )

    await state.clear()
    await callback.answer("Отправлено")
    if callback.message:
        await callback.message.edit_text(f"Сообщение отправлено в топик: {topic_title}.")
