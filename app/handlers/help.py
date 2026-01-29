"""Почему: справка и подсказки — единый источник для пользователей."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()


HELP_TEXT = (
    "Слежу за порядком в чате: мат и спам удаляю, флудеров мьючу — работа такая. "
    "В топике «шлагбаум» помогу оформить заявку, а вечерами (22-23 МСК) играю с вами в 21 (/21) и викторину (/umnij). "
    "Пиши, если что — я тут практически живу."
)


@router.message(Command("start"))
@router.message(Command("help"))
async def help_command(message: Message) -> None:
    logger.info("HANDLER: help_command")
    await message.reply(HELP_TEXT)
    logger.info("OUT: HELP_TEXT")


@router.message()
async def mention_help(message: Message, bot: Bot) -> None:
    logger.info(f"HANDLER: mention_help called, text={message.text!r}")
    if message.text is None:
        return
    me = await bot.get_me()
    username = me.username
    if username and f"@{username.lower()}" in message.text.lower():
        logger.info(f"HANDLER: mention_help MATCH @{username}")
        await message.reply(HELP_TEXT)
        logger.info("OUT: HELP_TEXT (mention)")
