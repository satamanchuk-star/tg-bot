"""Почему: общая логика для проверок администратора и извлечения целей."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import Message


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"administrator", "creator"}


def extract_target_user(message: Message) -> tuple[int | None, str | None]:
    """Получает цель из реплая."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return (
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.full_name,
        )
    return None, None
