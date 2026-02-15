"""Почему: базовая модерация изолирована, чтобы не смешивать с играми и анкетами."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import ChatPermissions, Message

from app.config import settings
from app.db import get_session
from app.models import FloodRecord, MessageLog, ModerationEvent
from app.services.ai_module import get_ai_client
from app.services.flood import FloodTracker
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import is_admin
from app.utils.text import contains_forbidden_link

logger = logging.getLogger(__name__)
router = Router()
FLOOD_TRACKER = FloodTracker(limit=10, window_seconds=120)


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


async def _store_message_log(message: Message, severity: int) -> None:
    if message.from_user is None:
        return
    async for session in get_session():
        session.add(
            MessageLog(
                chat_id=message.chat.id,
                topic_id=message.message_thread_id,
                user_id=message.from_user.id,
                text=message.text,
                severity=severity,
            )
        )
        await session.commit()


async def _store_mod_event(chat_id: int, user_id: int, event_type: str, severity: int) -> None:
    async for session in get_session():
        session.add(
            ModerationEvent(
                chat_id=chat_id,
                user_id=user_id,
                event_type=event_type,
                severity=severity,
            )
        )
        await session.commit()


@router.message(Command("rules"))
async def send_rules(message: Message) -> None:
    await message.reply("Пожалуйста, прочитай правила в закрепленном сообщении.")


@router.message(StateFilter(None), flags={"block": False})
async def moderate_message(message: Message, bot: Bot) -> None:
    """Модерация сообщений. Пропускает пользователей в FSM-состоянии (заполняют форму)."""
    if message.chat.id != settings.forum_chat_id:
        return
    if message.from_user is None or message.text is None:
        return
    if await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return

    text = message.text
    ai_client = get_ai_client()
    decision = await ai_client.moderate(text)
    await _store_message_log(message, decision.severity)

    if decision.severity >= 1:
        await _store_mod_event(message.chat.id, message.from_user.id, "warn", decision.severity)

    if decision.action in {"delete_warn", "delete_strike"}:
        await message.delete()
        await _store_mod_event(message.chat.id, message.from_user.id, "delete", decision.severity)

    if decision.action == "warn":
        await _warn_user(message, "пожалуйста, без грубости. Давайте общаться уважительно.", bot)
        return

    if decision.action == "delete_warn":
        await _warn_user(message, "сообщение удалено из-за нарушения правил. Без повторов, пожалуйста.", bot)
        return

    if decision.action == "delete_strike":
        async for session in get_session():
            strike_count = await add_strike(session, message.from_user.id, settings.forum_chat_id)
            await session.commit()
        await _store_mod_event(message.chat.id, message.from_user.id, "strike", decision.severity)
        await _warn_user(
            message,
            f"зафиксирован страйк {strike_count}/3. Соблюдайте правила общения.",
            bot,
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
                await clear_strikes(session, message.from_user.id, settings.forum_chat_id)
                await session.commit()
            await _warn_user(message, "3 страйка = мут на 24 часа.", bot)
        return

    if contains_forbidden_link(text):
        await message.delete()
        await _warn_user(message, "ссылки разрешены только в формате Telegram.", bot)
        await _store_mod_event(message.chat.id, message.from_user.id, "delete", 1)
        return

    count = FLOOD_TRACKER.register(message.from_user.id, settings.forum_chat_id, datetime.utcnow())
    if count <= 10:
        return

    async for session in get_session():
        record = await session.get(
            FloodRecord,
            {"user_id": message.from_user.id, "chat_id": settings.forum_chat_id},
        )
        now = datetime.utcnow()
        if record is None:
            record = FloodRecord(user_id=message.from_user.id, chat_id=settings.forum_chat_id)
            session.add(record)
        repeat_within_hour = record.last_flood_at and now - record.last_flood_at < timedelta(hours=1)
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
    await _warn_user(message, f"слишком частые сообщения. Мут на {mute_minutes} минут.", bot)
    await _store_mod_event(message.chat.id, message.from_user.id, "mute", 2)
