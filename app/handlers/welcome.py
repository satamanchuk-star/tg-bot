"""Почему: автоприветствие новичков с кнопками топиков снижает порог входа."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.config import settings

router = Router()
logger = logging.getLogger(__name__)

_WELCOME_TEXT = (
    "Добро пожаловать в чат ЖК «Живописный»! 🏠\n\n"
    "Здесь живут ваши соседи — задавайте вопросы, делитесь новостями.\n"
    "Перейдите в нужный раздел:"
)


def _welcome_keyboard() -> InlineKeyboardMarkup:
    topics: list[tuple[str, int | None]] = [
        ("📋 Правила", settings.topic_rules),
        ("📢 Важное", settings.topic_important),
        ("🔧 Ремонт", settings.topic_repair),
        ("😾 Жалобы", settings.topic_complaints),
        ("🐾 Питомцы", settings.topic_pets),
        ("🏡 Недвижимость", settings.topic_realty),
        ("🛒 Барахолка", settings.topic_market),
        ("👪 Мамы и папы", settings.topic_parents),
    ]

    chat_id = settings.forum_chat_id
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for label, topic_id in topics:
        if topic_id is None:
            continue
        url = f"https://t.me/c/{str(chat_id).lstrip('-100')}/{topic_id}"
        btn = InlineKeyboardButton(text=label, url=url)
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.id != settings.forum_chat_id:
        return
    if event.new_chat_member.user.is_bot:
        return

    keyboard = _welcome_keyboard()
    if not keyboard.inline_keyboard:
        return

    try:
        await bot.send_message(
            event.chat.id,
            _WELCOME_TEXT,
            reply_markup=keyboard,
        )
        logger.info(
            "WELCOME: новый участник %s (id=%s) в чате %s",
            event.new_chat_member.user.full_name,
            event.new_chat_member.user.id,
            event.chat.id,
        )
    except Exception:
        logger.warning("Не удалось отправить приветствие новому участнику.", exc_info=True)
