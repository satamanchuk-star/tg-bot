"""Почему: доработки бота — осмысленная трата монет жителями."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import get_session
from app.services.improvements import (
    IMPROVEMENT_CREATE_COST,
    IMPROVEMENT_THRESHOLD,
    IMPROVEMENT_VOTE_COST,
    IMPROVEMENT_LIFETIME_DAYS,
    can_create_improvement_this_month,
    create_improvement,
    get_active_improvements,
    vote_for_improvement,
)

router = Router()
logger = logging.getLogger(__name__)

# in-memory: user_id → ожидает текст доработки
_PENDING_IMPROVEMENT: dict[int, bool] = {}


class _PendingImprovementFilter(BaseFilter):
    """Фильтр: пропускает только сообщения от пользователей в режиме ожидания описания доработки."""
    async def __call__(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        return message.from_user.id in _PENDING_IMPROVEMENT


# ──────────────────────────────────────────────────────────
# ДОРАБОТКИ БОТА
# ──────────────────────────────────────────────────────────

def _improvement_keyboard(improvement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Поддержать ({IMPROVEMENT_VOTE_COST} монет)",
            callback_data=f"impr_vote:{improvement_id}",
        )
    ]])


@router.message(Command("доработка", "improvement"))
async def improvement_command(message: Message) -> None:
    """Начать создание доработки бота. Ограничение: 1 раз в месяц."""
    if message.from_user is None:
        return

    user_id = message.from_user.id

    async for session in get_session():
        can_create = await can_create_improvement_this_month(
            session, user_id, settings.forum_chat_id
        )

    if not can_create:
        await message.reply(
            "В этом месяце вы уже подавали доработку.\n"
            "Можно подавать одну доработку в месяц. Возвращайтесь в следующем!\n\n"
            "Поддержите чужие идеи: /доработки"
        )
        return

    # Помечаем что ждём ввод
    _PENDING_IMPROVEMENT[user_id] = True

    await message.reply(
        "Я развиваюсь и расту!\n\n"
        "Опиши подробно: какую доработку в боте ты хотел бы видеть?\n"
        "Чем точнее — тем больше шансов, что другие жители поддержат.\n\n"
        f"Стоимость подачи: {IMPROVEMENT_CREATE_COST} монет\n"
        f"Порог поддержки: {IMPROVEMENT_THRESHOLD} монет\n"
        f"Срок голосования: {IMPROVEMENT_LIFETIME_DAYS} дней\n\n"
        "Напиши описание одним сообщением:"
    )


@router.message(_PendingImprovementFilter(), F.text)
async def improvement_text_handler(message: Message, bot: Bot) -> None:
    """Принимает текст доработки от пользователя в режиме ожидания."""
    user_id = message.from_user.id
    # Пропускаем команды
    text = (message.text or "").strip()
    if text.startswith("/"):
        del _PENDING_IMPROVEMENT[user_id]
        return

    del _PENDING_IMPROVEMENT[user_id]

    if len(text) < 20:
        await message.reply(
            "Слишком коротко — опиши идею подробнее (минимум 20 символов).\n"
            "Начни заново: /доработка"
        )
        return

    user_name = message.from_user.full_name
    improvement = None
    async for session in get_session():
        # Перепроверяем лимит (защита от гонки)
        can_create = await can_create_improvement_this_month(
            session, user_id, settings.forum_chat_id
        )
        if not can_create:
            await message.reply("В этом месяце вы уже подавали доработку.")
            return

        result, extra = await create_improvement(
            session,
            chat_id=settings.forum_chat_id,
            author_id=user_id,
            author_name=user_name,
            text=text,
        )
        if result is None:
            reason = extra
            balance = int(reason.split(":")[1]) if ":" in reason else 0
            await message.reply(
                f"Недостаточно монет.\n"
                f"Нужно: {IMPROVEMENT_CREATE_COST} монет, у вас: {balance}.\n"
                f"Зарабатывайте в /21 и рулетке."
            )
            return
        improvement = result
        new_balance = extra
        await session.commit()

    expires_str = improvement.expires_at.strftime("%d.%m.%Y")
    sent = await message.answer(
        f"Доработка #{improvement.id} подана!\n\n"
        f"«{improvement.text[:300]}»\n\n"
        f"Автор: {user_name}\n"
        f"Собрано: {improvement.coins_total} / {improvement.threshold} монет\n"
        f"Голосование до: {expires_str}\n"
        f"Ваш остаток: {new_balance} монет\n\n"
        f"Жители, поддержите доработку кнопкой!",
        reply_markup=_improvement_keyboard(improvement.id),
    )
    logger.info("IMPROVEMENT #%d создана пользователем %s", improvement.id, user_id)


@router.message(Command("доработки", "improvements"))
async def improvements_list_command(message: Message) -> None:
    """Показывает список активных доработок бота."""
    async for session in get_session():
        improvements = await get_active_improvements(session, settings.forum_chat_id)

    if not improvements:
        await message.reply(
            f"Активных доработок нет.\n\n"
            f"Предложите свою: /доработка\n"
            f"Стоимость: {IMPROVEMENT_CREATE_COST} монет | Ограничение: 1 в месяц"
        )
        return

    lines = ["Доработки бота — голосуй монетами:\n"]
    for imp in improvements:
        bar_filled = min(10, int(imp.coins_total / imp.threshold * 10))
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        pct = min(100, int(imp.coins_total / imp.threshold * 100))
        _exp = imp.expires_at if imp.expires_at.tzinfo else imp.expires_at.replace(tzinfo=__import__("datetime").timezone.utc)
        days_left = max(0, (_exp - __import__("datetime").datetime.now(__import__("datetime").timezone.utc)).days)
        lines.append(
            f"#{imp.id} [{bar}] {pct}%\n"
            f"«{imp.text[:120]}»\n"
            f"Автор: {imp.author_name} | {imp.coins_total}/{imp.threshold} монет | осталось {days_left} дн.\n"
        )

    lines.append(f"Поддержать кнопкой под сообщением доработки. Голос = {IMPROVEMENT_VOTE_COST} монет.")
    await message.reply("\n".join(lines))


@router.callback_query(F.data.startswith("impr_vote:"))
async def improvement_vote_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Обрабатывает голос за доработку через inline-кнопку."""
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return

    try:
        improvement_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных.")
        return

    user_id = callback.from_user.id
    user_name = callback.from_user.full_name

    improvement = None
    just_completed = False
    async for session in get_session():
        result, extra, just_completed = await vote_for_improvement(
            session,
            improvement_id=improvement_id,
            user_id=user_id,
            user_name=user_name,
            chat_id=settings.forum_chat_id,
        )

        if result is None:
            reason = extra
            if reason == "already_voted":
                await callback.answer("Вы уже поддержали эту доработку.", show_alert=False)
            elif reason == "already_completed":
                await callback.answer("Доработка уже принята в работу!", show_alert=False)
            elif reason == "expired":
                await callback.answer("Срок голосования истёк.", show_alert=True)
            elif reason == "not_found":
                await callback.answer("Доработка не найдена.", show_alert=True)
            else:
                balance = int(reason.split(":")[1]) if ":" in reason else 0
                await callback.answer(
                    f"Недостаточно монет. У вас: {balance}, нужно: {IMPROVEMENT_VOTE_COST}.",
                    show_alert=True,
                )
            return

        improvement = result
        new_balance = extra
        await session.commit()

    await callback.answer(f"Поддержали! Остаток: {new_balance} монет", show_alert=False)

    bar_filled = min(10, int(improvement.coins_total / improvement.threshold * 10))
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    pct = min(100, int(improvement.coins_total / improvement.threshold * 100))

    if just_completed:
        try:
            await callback.message.edit_text(
                f"Доработка #{improvement.id} ПРИНЯТА В РАБОТУ!\n\n"
                f"«{improvement.text}»\n\n"
                f"Автор: {improvement.author_name}\n"
                f"Собрано: {improvement.coins_total} из {improvement.threshold} монет\n\n"
                f"Доработка взята в разработку!",
                reply_markup=None,
            )
        except Exception:
            logger.warning("Не удалось обновить сообщение доработки #%d", improvement.id)

        try:
            await bot.send_message(
                settings.forum_chat_id,
                f"ДОРАБОТКА ПРИНЯТА В РАБОТУ!\n\n"
                f"«{improvement.text}»\n\n"
                f"Жители ЖК поддержали {improvement.coins_total} монетами!\n"
                f"Автор: {improvement.author_name}",
                message_thread_id=callback.message.message_thread_id,
            )
        except Exception:
            logger.warning("Не удалось отправить объявление о доработке #%d", improvement.id)
    else:
        expires_str = improvement.expires_at.strftime("%d.%m.%Y")
        try:
            await callback.message.edit_text(
                f"Доработка #{improvement.id}\n\n"
                f"«{improvement.text[:300]}»\n\n"
                f"Автор: {improvement.author_name}\n"
                f"[{bar}] {pct}%\n"
                f"Собрано: {improvement.coins_total} / {improvement.threshold} монет\n"
                f"До: {expires_str}\n\n"
                f"Поддержите доработку!",
                reply_markup=_improvement_keyboard(improvement.id),
            )
        except Exception:
            pass
