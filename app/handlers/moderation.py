"""Почему: базовая модерация изолирована, чтобы не смешивать с играми и анкетами."""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import ChatPermissions, Message, MessageReactionUpdated

from sqlalchemy import and_, select

from app.config import settings
from app.db import get_session
from app.models import FloodRecord, MessageLog, ModerationEvent, ModerationTraining
from app.services.ai_module import get_ai_client
from app.services.flood import FloodTracker
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import is_admin
from app.utils.text import contains_forbidden_link

logger = logging.getLogger(__name__)
router = Router()
FLOOD_TRACKER = FloodTracker(limit=10, window_seconds=120)

# Множество message_id, уже прошедших модерацию (предотвращает двойной вызов)
_MODERATED_MSG_IDS: set[int] = set()
_MODERATED_MSG_IDS_MAX = 500

# Вариативные мягкие предупреждения (L1)
_SOFT_WARNINGS = (
    "давайте мягче 🙂",
    "чуть полегче, пожалуйста 🙌",
    "тут все соседи — давайте дружелюбнее!",
    "понимаю эмоции, но давайте без резкости 😊",
    "осторожнее с формулировками — тут все свои.",
    "полегче с тоном, пожалуйста. Мы же соседи!",
)

# Вариативные жёсткие предупреждения (L2)
_HARD_WARNINGS = (
    "это предупреждение ({count}/3). Пожалуйста, соблюдайте правила.",
    "получаете предупреждение ({count}/3). Давайте без нарушений.",
    "предупреждение номер {count} из 3. Пожалуйста, будьте корректнее.",
    "это {count}-е предупреждение из 3. Правила действуют для всех.",
)


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


async def _get_topic_context(chat_id: int, topic_id: int | None, limit: int = 10) -> list[str]:
    """Возвращает последние сообщения из того же топика для контекстной модерации.

    Формат: «[user_NNNN]: текст» — чтобы AI видел, кто что написал.
    """
    if topic_id is None:
        return []
    try:
        async for session in get_session():
            result = await session.execute(
                select(MessageLog.user_id, MessageLog.text)
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
            rows = result.all()
            return [f"[user_{uid}]: {txt}" for uid, txt in reversed(rows)]
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

    # Предотвращаем двойную модерацию одного сообщения (mention_help + moderate_message)
    msg_id = message.message_id
    if msg_id in _MODERATED_MSG_IDS:
        return False
    if len(_MODERATED_MSG_IDS) > _MODERATED_MSG_IDS_MAX:
        to_remove = sorted(_MODERATED_MSG_IDS)[:_MODERATED_MSG_IDS_MAX // 2]
        for mid in to_remove:
            _MODERATED_MSG_IDS.discard(mid)
    _MODERATED_MSG_IDS.add(msg_id)

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

    # Добавляем текущее сообщение с user_id для полного контекста
    current_msg = f"[user_{user_id}]: {text}"

    if settings.ai_feature_moderation:
        ai_client = get_ai_client()
        decision = await ai_client.moderate(
            current_msg, chat_id=chat_id, context=topic_context,
        )
    else:
        from app.services.ai_module import local_moderation
        decision = local_moderation(current_msg)
    severity = decision.severity
    violation_type = getattr(decision, "violation_type", None)
    confidence = getattr(decision, "confidence", None)
    sentiment = getattr(decision, "sentiment", "neutral")

    await _store_message_log(message, severity, sentiment=sentiment)

    # Записываем sentiment в буфер настроения чата
    if sentiment:
        try:
            from app.services.mood import record_sentiment
            record_sentiment(chat_id, message.message_thread_id, sentiment)
        except Exception:
            pass

    # Режим тихого обучения: не модерируем, а отправляем в лог-чат для разметки
    if settings.moderation_training_mode:
        if severity >= 1:
            await _send_training_sample(message, bot, severity, violation_type, confidence)
        return False

    # L0: ничего
    if severity == 0:
        # Flood-проверка (не связана с AI severity)
        return await _check_flood(message, bot)

    # L1: мягкое предупреждение, без счётчика
    if severity == 1:
        await _warn_user(message, random.choice(_SOFT_WARNINGS), bot)
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
        warn_text = random.choice(_HARD_WARNINGS).format(count=strike_count)
        await _warn_user(message, warn_text, bot)
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


# ---------------------------------------------------------------------------
# Тихое обучение: отправка подозрительных сообщений в лог-чат для разметки
# ---------------------------------------------------------------------------

_TRAINING_VIOLATION_LABELS = {
    "profanity": "мат",
    "rude": "грубость",
    "aggression": "агрессия",
}


async def _send_training_sample(
    message: Message,
    bot: Bot,
    severity: int,
    violation_type: str | None,
    confidence: float | None,
) -> None:
    """Отправляет сообщение в лог-чат с вопросом для разметки реакциями."""
    if message.from_user is None or message.text is None:
        return

    vtype_label = _TRAINING_VIOLATION_LABELS.get(violation_type or "", violation_type or "н/д")
    conf_pct = f"{confidence * 100:.0f}%" if confidence is not None else "н/д"

    log_text = (
        f"🔍 <b>Обучение модерации</b>\n\n"
        f"<b>Текст:</b> {message.text[:500]}\n\n"
        f"<b>AI считает:</b> {vtype_label} (severity {severity}, уверенность {conf_pct})\n\n"
        f"Это нарушение? Поставьте реакцию:\n"
        f"👍 — да, это грубость/мат\n"
        f"👎 — нет, всё нормально"
    )
    try:
        sent = await bot.send_message(
            settings.admin_log_chat_id,
            log_text,
            parse_mode="HTML",
        )
        # Сохраняем образец в БД
        async for session in get_session():
            session.add(
                ModerationTraining(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    message_text=message.text[:2000],
                    ai_severity=severity,
                    ai_violation_type=violation_type,
                    ai_confidence=confidence,
                    log_message_id=sent.message_id,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Не удалось отправить обучающий образец в лог-чат")


@router.message_reaction(F.chat.id == settings.admin_log_chat_id)
async def handle_training_reaction(event: MessageReactionUpdated) -> None:
    """Обрабатывает реакции на обучающие сообщения в лог-чате."""
    if not settings.moderation_training_mode:
        return
    if event.user is None:
        return

    log_msg_id = event.message_id
    voter_id = event.user.id

    # Определяем какая реакция поставлена
    new_emojis: set[str] = set()
    for reaction in event.new_reaction or []:
        emoji = getattr(reaction, "emoji", None)
        if emoji:
            new_emojis.add(emoji)

    is_yes = "👍" in new_emojis
    is_no = "👎" in new_emojis
    if not is_yes and not is_no:
        return

    try:
        async for session in get_session():
            result = await session.execute(
                select(ModerationTraining).where(
                    ModerationTraining.log_message_id == log_msg_id,
                )
            )
            sample = result.scalar_one_or_none()
            if sample is None:
                return

            # Проверяем, не голосовал ли уже этот пользователь
            voted_ids: list[int] = []
            if sample.voted_user_ids:
                try:
                    voted_ids = json.loads(sample.voted_user_ids)
                except (json.JSONDecodeError, TypeError):
                    voted_ids = []
            if voter_id in voted_ids:
                return

            voted_ids.append(voter_id)
            sample.voted_user_ids = json.dumps(voted_ids)
            if is_yes:
                sample.vote_yes += 1
            if is_no:
                sample.vote_no += 1
            await session.commit()
            logger.info(
                "Обучение: msg_id=%d, voter=%d, yes=%d, no=%d",
                log_msg_id, voter_id, sample.vote_yes, sample.vote_no,
            )
    except Exception:
        logger.exception("Ошибка при обработке реакции на обучающее сообщение")


@router.message(StateFilter(None), flags={"block": False})
async def moderate_message(message: Message, bot: Bot) -> None:
    """Модерация сообщений. Пропускает пользователей в FSM-состоянии (заполняют форму)."""
    moderated = await run_moderation(message, bot)

    # Регистрируем активность топика для проактивного сервиса
    if message.chat.id == settings.forum_chat_id:
        try:
            from app.services.proactive import register_message_activity
            register_message_activity(message.chat.id, message.message_thread_id)
        except Exception:
            pass

    # Проактивные ответы и комментарии отключены:
    # maybe_proactive_reply — бот отвечал на вопросы, которые ему не задавали
    # maybe_topic_comment — бот вклинивался в обсуждения без приглашения
    # Пользователи могут @-упомянуть бота, когда хотят его помощи
