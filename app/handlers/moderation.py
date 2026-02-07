"""Почему: базовая модерация изолирована, чтобы не смешивать с играми и анкетами."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import ChatPermissions, Message

from app.config import settings
from app.db import get_session
from app.models import FloodRecord
from app.services.flood import FloodTracker
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import is_admin
from app.utils.profanity import (
    load_profanity,
    load_profanity_exceptions,
    split_profanity_words,
)
from app.utils.text import contains_forbidden_link, contains_profanity, normalize_words

logger = logging.getLogger(__name__)
router = Router()

PROFANITY_WORDS = load_profanity()
PROFANITY_EXCEPTIONS = load_profanity_exceptions()
EXACT_PROFANITY, PREFIX_PROFANITY = split_profanity_words(PROFANITY_WORDS)
logger.info(
    "Loaded %s profanity words, %s exceptions",
    len(PROFANITY_WORDS),
    len(PROFANITY_EXCEPTIONS),
)
FLOOD_TRACKER = FloodTracker(limit=10, window_seconds=120)


def update_profanity(words: set[str]) -> None:
    global PROFANITY_WORDS, EXACT_PROFANITY, PREFIX_PROFANITY
    PROFANITY_WORDS = set(words)
    EXACT_PROFANITY, PREFIX_PROFANITY = split_profanity_words(PROFANITY_WORDS)


def update_profanity_exceptions(words: set[str]) -> None:
    global PROFANITY_EXCEPTIONS
    PROFANITY_EXCEPTIONS = set(words)


async def _warn_user(message: Message, text: str, bot: Bot) -> None:
    if message.from_user is None:
        return
    mention = message.from_user.mention_html()
    await bot.send_message(
        message.chat.id,
        f"{mention}, {text}",
        parse_mode="HTML",
        message_thread_id=message.message_thread_id,
    )


@router.message(Command("rules"))
async def send_rules(message: Message) -> None:
    await message.reply("Пожалуйста, прочитай правила в закрепленном сообщении.")


@router.message(StateFilter(None), flags={"block": False})
async def moderate_message(message: Message, bot: Bot) -> None:
    """Модерация сообщений. Пропускает пользователей в FSM-состоянии (заполняют форму)."""
    logger.info(f"HANDLER: moderate_message, chat={message.chat.id}, text={message.text!r}")
    if message.chat.id != settings.forum_chat_id:
        logger.info(f"SKIP: wrong chat {message.chat.id} != {settings.forum_chat_id}")
        return
    if message.from_user is None or message.text is None:
        logger.info("SKIP: no user or text")
        return
    if await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        logger.info("SKIP: user is admin")
        return

    text = message.text
    words = normalize_words(text)
    logger.info(
        "Normalized words: %s, profanity check: %s",
        words,
        [word for word in words if word in PROFANITY_WORDS],
    )
    if contains_profanity(
        words,
        EXACT_PROFANITY,
        PREFIX_PROFANITY,
        PROFANITY_EXCEPTIONS,
    ):
        await message.delete()
        async for session in get_session():
            strike_count = await add_strike(
                session, message.from_user.id, settings.forum_chat_id
            )
            await session.commit()
        await _warn_user(
            message,
            f"плохие слова тут запрещены. Страйк {strike_count}/3. Прочти правила!",
            bot,
        )
        user = message.from_user
        username = f"@{user.username}" if user.username else user.full_name
        admin_log = (
            f"#мат\n"
            f"Чат: {message.chat.id}\n"
            f"Пользователь: {username} ({user.id})\n"
            f"Страйк: {strike_count}/3\n"
            f"Текст: {text}"
        )
        await bot.send_message(settings.admin_log_chat_id, admin_log)
        if strike_count >= 3:
            until = datetime.utcnow() + timedelta(hours=24)
            permissions = ChatPermissions(can_send_messages=False)
            await bot.restrict_chat_member(
                settings.forum_chat_id,
                message.from_user.id,
                permissions=permissions,
                until_date=until,
            )
            async for session in get_session():
                await clear_strikes(
                    session, message.from_user.id, settings.forum_chat_id
                )
                await session.commit()
            await _warn_user(message, "3 страйка = мут на 24 часа. Остынь.", bot)
        return

    if contains_forbidden_link(text):
        await message.delete()
        await _warn_user(
            message, "ссылки разрешены только телеграм. Прочти правила!", bot
        )
        user = message.from_user
        username = f"@{user.username}" if user.username else user.full_name
        admin_log = (
            f"#ссылка\n"
            f"Чат: {message.chat.id}\n"
            f"Пользователь: {username} ({user.id})\n"
            f"Текст: {text}"
        )
        await bot.send_message(settings.admin_log_chat_id, admin_log)
        return

    count = FLOOD_TRACKER.register(
        message.from_user.id, settings.forum_chat_id, datetime.utcnow()
    )
    if count > 10:
        async for session in get_session():
            record = await session.get(
                FloodRecord,
                {"user_id": message.from_user.id, "chat_id": settings.forum_chat_id},
            )
            now = datetime.utcnow()
            if record is None:
                record = FloodRecord(
                    user_id=message.from_user.id, chat_id=settings.forum_chat_id
                )
                session.add(record)
            repeat_within_hour = (
                record.last_flood_at and now - record.last_flood_at < timedelta(hours=1)
            )
            record.last_flood_at = now
            await session.commit()
        mute_minutes = 60 if repeat_within_hour else 15
        until = datetime.utcnow() + timedelta(minutes=mute_minutes)
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            settings.forum_chat_id,
            message.from_user.id,
            permissions=permissions,
            until_date=until,
        )
        await _warn_user(
            message,
            f"слишком часто пишешь. Мут на {mute_minutes} минут. Остынь!",
            bot,
        )
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Антифлуд: {message.from_user.id} мут на {mute_minutes} минут",
        )
