"""Почему: общая логика для проверок администратора и извлечения целей."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger(__name__)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"administrator", "creator"}


async def is_admin_message(bot: Bot, chat_id: int, message: Message) -> bool:
    """Проверяет права администратора для сообщения, включая анонимных админов."""
    if message.from_user is None:
        return bool(message.sender_chat and message.sender_chat.id == chat_id)
    try:
        return await is_admin(bot, chat_id, message.from_user.id)
    except Exception:  # noqa: BLE001 - не выдаём доступ при ошибке проверки
        logger.exception("Не удалось проверить права администратора.")
        return False


def extract_target_user(message: Message) -> tuple[int | None, str | None]:
    """Получает цель из реплая."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return (
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.full_name,
        )
    return None, None
