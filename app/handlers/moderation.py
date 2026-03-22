"""Почему: базовая модерация изолирована, чтобы не смешивать с играми и анкетами."""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageReactionUpdated,
)

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

# Runtime-флаг режима обучения (переопределяет settings.moderation_training_mode)
_TRAINING_MODE_OVERRIDE: bool | None = None


def set_training_mode(enabled: bool) -> None:
    """Включает/выключает режим обучения без перезапуска бота."""
    global _TRAINING_MODE_OVERRIDE
    _TRAINING_MODE_OVERRIDE = enabled
    logger.info("Training mode runtime: %s", "вкл" if enabled else "выкл")


def is_training_mode() -> bool:
    """Возвращает текущий статус режима обучения."""
    if _TRAINING_MODE_OVERRIDE is not None:
        return _TRAINING_MODE_OVERRIDE
    return settings.moderation_training_mode


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
    if is_training_mode():
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
        until = datetime.now(timezone.utc) + timedelta(hours=24)
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
        until = datetime.now(timezone.utc) + timedelta(hours=24)
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
    count = FLOOD_TRACKER.register(message.from_user.id, settings.forum_chat_id, datetime.now(timezone.utc))
    if count <= 10:
        return False

    async for session in get_session():
        record = await session.get(
            FloodRecord,
            {"user_id": message.from_user.id, "chat_id": settings.forum_chat_id},
        )
        now = datetime.now(timezone.utc)
        if record is None:
            record = FloodRecord(user_id=message.from_user.id, chat_id=settings.forum_chat_id)
            session.add(record)
        repeat_within_hour = record.last_flood_at and now - record.last_flood_at < timedelta(hours=1)
        record.last_flood_at = now
        await session.commit()

    mute_minutes = 60 if repeat_within_hour else 15
    until = datetime.now(timezone.utc) + timedelta(minutes=mute_minutes)
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

_TRAINING_ACTION_LABELS = {
    1: "предупреждение",
    2: "предупреждение + страйк",
    3: "удаление + мут 24ч",
}


def _build_training_keyboard(sample_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚠️ Предупредить", callback_data=f"train:warn:{sample_id}"),
            InlineKeyboardButton(text="🗑 Удалить + мут", callback_data=f"train:delete:{sample_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Всё нормально", callback_data=f"train:ok:{sample_id}"),
        ],
    ])


async def _send_training_sample(
    message: Message,
    bot: Bot,
    severity: int,
    violation_type: str | None,
    confidence: float | None,
) -> None:
    """Отправляет сообщение в лог-чат с inline-кнопками для подтверждения действия."""
    if message.from_user is None or message.text is None:
        return

    vtype_label = _TRAINING_VIOLATION_LABELS.get(violation_type or "", violation_type or "н/д")
    conf_pct = f"{confidence * 100:.0f}%" if confidence is not None else "н/д"
    action_label = _TRAINING_ACTION_LABELS.get(severity, f"severity {severity}")
    mention = message.from_user.mention_html()

    log_text = (
        f"🔍 <b>Обучение модерации</b>\n\n"
        f"<b>Пользователь:</b> {mention} (id={message.from_user.id})\n"
        f"<b>Текст:</b> {message.text[:500]}\n\n"
        f"🤖 <b>AI решение:</b> {vtype_label} (severity {severity}, уверенность {conf_pct})\n"
        f"<b>Я бы сделал:</b> {action_label}\n\n"
        f"Что сделать?"
    )
    try:
        # Сохраняем образец в БД (без keyboard — нужен ID)
        sample_id: int | None = None
        async for session in get_session():
            sample = ModerationTraining(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                message_text=message.text[:2000],
                ai_severity=severity,
                ai_violation_type=violation_type,
                ai_confidence=confidence,
                original_message_id=message.message_id,
            )
            session.add(sample)
            await session.commit()
            await session.refresh(sample)
            sample_id = sample.id

        if sample_id is None:
            logger.error("Не удалось сохранить обучающий образец в БД")
            return

        sent = await bot.send_message(
            settings.admin_log_chat_id,
            log_text,
            parse_mode="HTML",
            reply_markup=_build_training_keyboard(sample_id),
        )
        # Сохраняем message_id лог-сообщения
        async for session in get_session():
            result = await session.execute(
                select(ModerationTraining).where(ModerationTraining.id == sample_id)
            )
            sample = result.scalar_one_or_none()
            if sample:
                sample.log_message_id = sent.message_id
                await session.commit()
    except Exception:
        logger.exception("Не удалось отправить обучающий образец в лог-чат")


@router.callback_query(F.data.startswith("train:"))
async def handle_training_action(callback: CallbackQuery, bot: Bot) -> None:
    """Обрабатывает нажатие inline-кнопок на обучающих сообщениях."""
    if not is_training_mode():
        await callback.answer("Режим обучения выключен.", show_alert=False)
        return
    if callback.from_user is None or callback.data is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    _, action, sample_id_str = parts
    try:
        sample_id = int(sample_id_str)
    except ValueError:
        return

    try:
        async for session in get_session():
            result = await session.execute(
                select(ModerationTraining).where(ModerationTraining.id == sample_id)
            )
            sample = result.scalar_one_or_none()
            if sample is None:
                await callback.answer("Образец не найден в БД.", show_alert=True)
                return

            # Записываем фидбек
            voter_id = callback.from_user.id
            voted_ids: list[int] = []
            if sample.voted_user_ids:
                try:
                    voted_ids = json.loads(sample.voted_user_ids)
                except (json.JSONDecodeError, TypeError):
                    voted_ids = []
            if voter_id in voted_ids:
                await callback.answer("Вы уже проголосовали.", show_alert=False)
                return
            voted_ids.append(voter_id)
            sample.voted_user_ids = json.dumps(voted_ids)

            if action == "ok":
                sample.vote_no += 1
                await session.commit()
                await callback.answer("Записано: не нарушение ✅")
                result_label = "✅ Отклонено (не нарушение)"
            elif action == "warn":
                sample.vote_yes += 1
                await session.commit()
                # Отправляем предупреждение в форум
                try:
                    warn_text = random.choice(_HARD_WARNINGS).format(count="?")
                    await bot.send_message(
                        sample.chat_id,
                        f"<a href='tg://user?id={sample.user_id}'>Пользователь</a>, {warn_text}",
                        parse_mode="HTML",
                    )
                    await callback.answer("Предупреждение отправлено ⚠️")
                except Exception:
                    await callback.answer("Ошибка при отправке предупреждения", show_alert=True)
                    logger.exception("Ошибка retroactive warn")
                result_label = "⚠️ Предупреждение выдано"
            elif action == "delete":
                sample.vote_yes += 1
                await session.commit()
                # Удаляем сообщение из форума (если ещё есть)
                deleted = False
                if sample.original_message_id:
                    try:
                        await bot.delete_message(sample.chat_id, sample.original_message_id)
                        deleted = True
                    except Exception:
                        logger.info("Сообщение уже удалено или недоступно")
                # Мут 24ч
                try:
                    until = datetime.now(timezone.utc) + timedelta(hours=24)
                    permissions = ChatPermissions(can_send_messages=False)
                    await bot.restrict_chat_member(
                        sample.chat_id,
                        sample.user_id,
                        permissions=permissions,
                        until_date=until,
                    )
                    del_label = "удалено + " if deleted else ""
                    await callback.answer(f"Сообщение {del_label}мут 24ч 🗑")
                except Exception:
                    await callback.answer("Ошибка при муте пользователя", show_alert=True)
                    logger.exception("Ошибка retroactive delete/mute")
                result_label = "🗑 Удалено и мут"
            else:
                return

            logger.info(
                "Обучение: sample_id=%d, action=%s, voter=%d",
                sample_id, action, voter_id,
            )

        # Обновляем сообщение в лог-чате — убираем кнопки, добавляем итог
        if callback.message:
            try:
                original_text = callback.message.html_text or ""
                await callback.message.edit_text(
                    original_text + f"\n\n<b>Решение:</b> {result_label}",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass

    except Exception:
        logger.exception("Ошибка при обработке тренировочного callback")


@router.message(StateFilter(None), flags={"block": False})
async def moderate_message(message: Message, bot: Bot) -> None:
    """Модерация сообщений. Пропускает пользователей в FSM-состоянии (заполняют форму)."""
    moderated = await run_moderation(message, bot)

    # Регистрируем активность топика для проактивного сервиса
    if message.chat.id == settings.forum_chat_id:
        try:
            from app.services.proactive import (
                maybe_topic_comment,
                maybe_proactive_reply,
                maybe_welcome_newcomer,
                register_message_activity,
            )
            register_message_activity(message.chat.id, message.message_thread_id)
            # Приветствие новичков (первые сообщения в форуме)
            if not moderated:
                await maybe_welcome_newcomer(message, bot)
            # Подключаемся к активным дискуссиям
            await maybe_topic_comment(message, bot)
            # Проактивный ответ на вопросы (когда топик не активен)
            if not moderated:
                await maybe_proactive_reply(message, bot)
        except Exception:
            pass
