"""Почему: справка и подсказки — единый источник для пользователей."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()


HELP_TEXT = (
    "Я помощник чата ЖК. Что умею:\n"
    "• Удаляю мат, грубость и спам-ссылки.\n"
    "• Антифлуд: много сообщений подряд → мут.\n"
    "• В теме «шлагбаум» помогаю оформить заявку.\n"
    "• В теме «соседи» помогаю представить новичков.\n"
    "• В теме games играем в 21.\n\n"
    "Команды для всех:\n"
    "/help — помощь\n"
    "/rules — правила\n"
    "/21 — начать игру в 21 (topic games)\n"
    "/score — мои монеты (topic games)\n\n"
    "Топики чата доступны по темам форума."
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
