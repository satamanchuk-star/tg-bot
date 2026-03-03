"""Почему: базовая модерация изолирована, чтобы не смешивать с играми и анкетами."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import ChatPermissions, Message

from sqlalchemy import and_, select

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


async def _store_message_log(message: Message, severity: int, sentiment: str | None = None) -> None:
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
                sentiment=sentiment,
            )
        )
        await session.commit()


async def _get_topic_context(chat_id: int, topic_id: int | None, limit: int = 5) -> list[str]:
    """Возвращает последние сообщения из того же топика для контекстной модерации."""
    if topic_id is None:
        return []
    try:
        async for session in get_session():
            result = await session.execute(
                select(MessageLog.text)
                .where(
                    and_(
                        MessageLog.chat_id == chat_id,
                        MessageLog.topic_id == topic_id,
                        MessageLog.text.isnot(None),
                    )
                )
                .order_by(MessageLog.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return list(reversed(rows))
    except Exception:
        logger.warning("Не удалось загрузить контекст топика для модерации")
        return []


async def _store_mod_event(
    chat_id: int,
    user_id: int,
    event_type: str,
    severity: int,
    message_id: int | None = None,
    reason: str | None = None,
    confidence: float | None = None,
) -> None:
    async for session in get_session():
        session.add(
            ModerationEvent(
                chat_id=chat_id,
                user_id=user_id,
                event_type=event_type,
                severity=severity,
                message_id=message_id,
                reason=reason,
                confidence=confidence,
            )
        )
        await session.commit()


@router.message(Command("rules"))
async def send_rules(message: Message) -> None:
    await message.reply("Пожалуйста, прочитай правила в закрепленном сообщении.")


async def run_moderation(message: Message, bot: Bot) -> bool:
    """Проверяет сообщение на нарушения и применяет модерацию по severity.

    severity 0 (L0): ничего
    severity 1 (L1): мягкое предупреждение, без счётчика
    severity 2 (L2): жёсткое предупреждение + счётчик +1, БЕЗ удаления
    severity 3 (L3): удаление + счётчик +1 + немедленный мут + уведомление админа

    Пороги счётчика: 3 → мут 24ч, 5 → бан.

    Возвращает True, если сообщение было модерировано (severity >= 1).
    """
    if message.chat.id != settings.forum_chat_id:
        return False
    if message.from_user is None or message.text is None:
        return False
    if await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return False

    text = message.text
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Проверка запрещённых ссылок (до AI)
    if contains_forbidden_link(text):
        await message.delete()
        await _warn_user(message, "ссылки разрешены только в формате Telegram.", bot)
        await _store_mod_event(chat_id, user_id, "delete", 1, message_id=message.message_id)
        return True

    # Загружаем контекст разговора из того же топика
    topic_context = await _get_topic_context(chat_id, message.message_thread_id)

    ai_client = get_ai_client()
    decision = await ai_client.moderate(text, chat_id=chat_id, context=topic_context)
    severity = decision.severity
    violation_type = getattr(decision, "violation_type", None)
    confidence = getattr(decision, "confidence", None)
    sentiment = getattr(decision, "sentiment", "neutral")

    await _store_message_log(message, severity, sentiment=sentiment)

    # L0: ничего
    if severity == 0:
        # Flood-проверка (не связана с AI severity)
        return await _check_flood(message, bot)

    # L1: мягкое предупреждение, без счётчика
    if severity == 1:
        await _warn_user(message, "давайте мягче 🙂", bot)
        return True

    # L2: жёсткое предупреждение + счётчик +1, без удаления
    if severity == 2:
        async for session in get_session():
            strike_count = await add_strike(session, user_id, settings.forum_chat_id)
            await session.commit()
        await _store_mod_event(
            chat_id, user_id, "warn", severity,
            message_id=message.message_id, reason=violation_type, confidence=confidence,
        )
        await _warn_user(
            message,
            f"это предупреждение ({strike_count}/3). Пожалуйста, соблюдайте правила.",
            bot,
        )
        await _apply_strike_threshold(bot, message, user_id, strike_count)
        return True

    # L3: удаление + счётчик +1 + немедленный мут + уведомление админа
    if severity >= 3:
        await message.delete()
        async for session in get_session():
            strike_count = await add_strike(session, user_id, settings.forum_chat_id)
            await session.commit()
        await _store_mod_event(
            chat_id, user_id, "delete", severity,
            message_id=message.message_id, reason=violation_type, confidence=confidence,
        )
        # Немедленный мут 24ч
        until = datetime.utcnow() + timedelta(hours=24)
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            settings.forum_chat_id,
            user_id,
            permissions=permissions,
            until_date=until,
        )
        await _warn_user(message, "сообщение удалено, мут на 24 часа за грубое нарушение.", bot)
        # Уведомление админа
        mention = message.from_user.mention_html()
        admin_text = (
            f"🔴 L3 модерация\n"
            f"Пользователь: {mention} (id={user_id})\n"
            f"Причина: {violation_type or 'н/д'}\n"
            f"Уверенность: {confidence or 'н/д'}\n"
            f"Текст: {text[:200]}"
        )
        await bot.send_message(settings.admin_log_chat_id, admin_text, parse_mode="HTML")
        await _apply_strike_threshold(bot, message, user_id, strike_count)
        return True

    return False


async def _apply_strike_threshold(bot: Bot, message: Message, user_id: int, strike_count: int) -> None:
    """Применяет мут/бан по порогам счётчика предупреждений."""
    if strike_count >= 5:
        # Бан
        await bot.ban_chat_member(settings.forum_chat_id, user_id)
        async for session in get_session():
            await clear_strikes(session, user_id, settings.forum_chat_id)
            await session.commit()
        await _warn_user(message, "слишком много нарушений — бан.", bot)
    elif strike_count >= 3:
        # Мут 24ч
        until = datetime.utcnow() + timedelta(hours=24)
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            settings.forum_chat_id,
            user_id,
            permissions=permissions,
            until_date=until,
        )
        await _warn_user(message, "3 предупреждения — пауза в чате на 24 часа.", bot)


async def _check_flood(message: Message, bot: Bot) -> bool:
    """Flood-проверка (не связана с AI severity)."""
    if message.from_user is None:
        return False
    count = FLOOD_TRACKER.register(message.from_user.id, settings.forum_chat_id, datetime.utcnow())
    if count <= 10:
        return False

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
    return True


@router.message(StateFilter(None), flags={"block": False})
async def moderate_message(message: Message, bot: Bot) -> None:
    """Модерация сообщений. Пропускает пользователей в FSM-состоянии (заполняют форму)."""
    await run_moderation(message, bot)
