"""Почему: базовая модерация изолирована, чтобы не смешивать с играми и анкетами."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import ChatPermissions, Message

from app.config import settings
from app.db import get_session
from app.models import FloodRecord
from app.services.flood import FloodTracker
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import is_admin
from app.utils.profanity import load_profanity
from app.utils.text import contains_forbidden_link, normalize_words

router = Router()

PROFANITY_WORDS = load_profanity()
FLOOD_TRACKER = FloodTracker(limit=10, window_seconds=120)


def update_profanity(words: set[str]) -> None:
    PROFANITY_WORDS.clear()
    PROFANITY_WORDS.update(words)


async def _warn_user(message: Message, text: str, bot: Bot) -> None:
    if message.from_user is None:
        return
    await message.answer(
        f"{message.from_user.get_mention(as_html=True)}, {text}", parse_mode="HTML"
    )


@router.message(Command("rules"))
async def send_rules(message: Message) -> None:
    await message.reply("Пожалуйста, прочитай правила в закрепленном сообщении.")


@router.message()
async def moderate_message(message: Message, bot: Bot) -> None:
    if message.chat.id != settings.forum_chat_id:
        return
    if message.from_user is None or message.text is None:
        return
    if await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return

    text = message.text
    words = normalize_words(text)
    if any(word in PROFANITY_WORDS for word in words):
        await message.delete()
        async for session in get_session():
            strike_count = await add_strike(
                session, message.from_user.id, settings.forum_chat_id
            )
            await session.commit()
        await _warn_user(
            message,
            "плохие слова тут запрещены. Это страйк. Прочти правила!",
            bot,
        )
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Мат: {message.from_user.id} получил страйк ({strike_count}).",
        )
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
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Ссылка удалена у {message.from_user.id}",
        )
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
