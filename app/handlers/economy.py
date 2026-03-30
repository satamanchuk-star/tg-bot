"""Почему: лотерея и инициативы жителей — способ тратить монеты с реальным смыслом."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import get_session
from app.services.initiatives import (
    INITIATIVE_CREATE_COST,
    INITIATIVE_THRESHOLD,
    INITIATIVE_VOTE_COST,
    create_initiative,
    get_active_initiatives,
    vote_for_initiative,
)
from app.services.lottery import (
    TICKET_COST,
    buy_ticket,
    current_week_key,
    get_current_pot,
)

router = Router()
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# ЛОТЕРЕЯ
# ──────────────────────────────────────────────────────────

@router.message(Command("лотерея", "lottery"))
async def lottery_command(message: Message) -> None:
    """Купить лотерейный билет на текущую неделю."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    user_name = message.from_user.full_name

    async for session in get_session():
        pot, count = await get_current_pot(session, settings.forum_chat_id)

        result, extra = await buy_ticket(
            session,
            user_id=user_id,
            chat_id=settings.forum_chat_id,
            user_name=user_name,
        )

        if result is None:
            reason = extra
            if reason == "already_bought":
                await message.reply(
                    f"Вы уже купили билет на эту неделю!\n\n"
                    f"Банк недели: {pot} монет, участников: {count}\n"
                    f"Розыгрыш — воскресенье в 21:00."
                )
            else:
                balance = int(reason.split(":")[1]) if ":" in reason else 0
                await message.reply(
                    f"Недостаточно монет.\n"
                    f"Цена билета: {TICKET_COST} монет, у вас: {balance}.\n"
                    f"Зарабатывайте монеты в /21, викторине и рулетке."
                )
            return

        await session.commit()
        new_pot = pot + TICKET_COST
        new_count = count + 1

    await message.reply(
        f"Билет куплен!\n\n"
        f"Неделя: {current_week_key()}\n"
        f"Банк: {new_pot} монет среди {new_count} участников\n"
        f"Ваш остаток: {extra} монет\n\n"
        f"Розыгрыш — воскресенье в 21:00.\n"
        f"Чем больше участников — тем крупнее джекпот!\n"
        f"Узнать банк: /банк"
    )


@router.message(Command("банк", "jackpot"))
async def jackpot_command(message: Message) -> None:
    """Показывает текущий банк лотереи."""
    async for session in get_session():
        pot, count = await get_current_pot(session, settings.forum_chat_id)

    if count == 0:
        await message.reply(
            f"Лотерея этой недели ещё не началась.\n"
            f"Купите первый билет: /лотерея (стоимость: {TICKET_COST} монет)\n"
            f"Розыгрыш — воскресенье в 21:00."
        )
        return

    await message.reply(
        f"Лотерея недели {current_week_key()}\n\n"
        f"Банк: {pot} монет\n"
        f"Участников: {count}\n\n"
        f"Купить билет: /лотерея\n"
        f"Розыгрыш — воскресенье в 21:00."
    )


# ──────────────────────────────────────────────────────────
# ИНИЦИАТИВЫ ЖИТЕЛЕЙ
# ──────────────────────────────────────────────────────────

def _initiative_keyboard(initiative_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Поддержать ({INITIATIVE_VOTE_COST} монет)",
            callback_data=f"init_vote:{initiative_id}",
        )
    ]])


@router.message(Command("инициатива", "initiative"))
async def initiative_command(message: Message, bot: Bot) -> None:
    """Создаёт инициативу по улучшению ЖК. Формат: /инициатива <текст>"""
    if message.from_user is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            f"Создайте инициативу по улучшению нашего дома!\n\n"
            f"Формат: /инициатива <ваше предложение>\n"
            f"Например: /инициатива Поставить новую лавочку у подъезда 3\n\n"
            f"Стоимость создания: {INITIATIVE_CREATE_COST} монет\n"
            f"Порог поддержки: {INITIATIVE_THRESHOLD} монет\n"
            f"При достижении порога — бот объявит о принятии инициативы!"
        )
        return

    text = parts[1].strip()
    if len(text) < 10:
        await message.reply("Слишком коротко. Опишите инициативу подробнее.")
        return

    user_id = message.from_user.id
    user_name = message.from_user.full_name

    initiative = None
    async for session in get_session():
        result, extra = await create_initiative(
            session,
            chat_id=settings.forum_chat_id,
            author_id=user_id,
            author_name=user_name,
            text=text,
        )

        if result is None:
            reason = extra
            if reason == "too_many_active":
                await message.reply(
                    "Сейчас слишком много активных инициатив. "
                    "Поддержите существующие: /инициативы"
                )
            else:
                balance = int(reason.split(":")[1]) if ":" in reason else 0
                await message.reply(
                    f"Недостаточно монет.\n"
                    f"Нужно: {INITIATIVE_CREATE_COST} монет, у вас: {balance}.\n"
                    f"Зарабатывайте в /21, викторине и рулетке."
                )
            return

        initiative = result
        new_balance = extra
        await session.commit()

    sent = await message.answer(
        f"Новая инициатива #{initiative.id}!\n\n"
        f"«{initiative.text}»\n\n"
        f"Автор: {user_name}\n"
        f"Собрано: {initiative.coins_total} / {initiative.threshold} монет\n"
        f"Ваш остаток: {new_balance} монет\n\n"
        f"Жители, поддержите инициативу кнопкой!",
        reply_markup=_initiative_keyboard(initiative.id),
    )

    # Сохраняем message_id объявления для обновления при голосованиях
    async for session in get_session():
        ini = await session.get(type(initiative), initiative.id)
        if ini:
            ini.announcement_message_id = sent.message_id
            await session.commit()


@router.message(Command("инициативы", "initiatives"))
async def initiatives_list_command(message: Message) -> None:
    """Показывает список активных инициатив жителей."""
    async for session in get_session():
        initiatives = await get_active_initiatives(session, settings.forum_chat_id)

    if not initiatives:
        await message.reply(
            f"Активных инициатив нет.\n\n"
            f"Создайте первую: /инициатива <ваше предложение>\n"
            f"Стоимость: {INITIATIVE_CREATE_COST} монет"
        )
        return

    lines = ["Активные инициативы жителей:\n"]
    for ini in initiatives:
        bar_filled = int(ini.coins_total / ini.threshold * 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        pct = int(ini.coins_total / ini.threshold * 100)
        lines.append(
            f"#{ini.id} [{bar}] {pct}%\n"
            f"«{ini.text[:100]}»\n"
            f"Собрано: {ini.coins_total}/{ini.threshold} монет\n"
        )

    lines.append(f"Поддержать: /инициатива_голос <номер>  ({INITIATIVE_VOTE_COST} монет)")
    await message.reply("\n".join(lines))


@router.callback_query(F.data.startswith("init_vote:"))
async def initiative_vote_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Обрабатывает голос за инициативу через inline-кнопку."""
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return

    try:
        initiative_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных.")
        return

    user_id = callback.from_user.id
    user_name = callback.from_user.full_name

    just_completed = False
    initiative = None
    async for session in get_session():
        result, extra, just_completed = await vote_for_initiative(
            session,
            initiative_id=initiative_id,
            user_id=user_id,
            user_name=user_name,
            chat_id=settings.forum_chat_id,
        )

        if result is None:
            reason = extra
            if reason == "already_voted":
                await callback.answer("Вы уже поддержали эту инициативу.", show_alert=False)
            elif reason == "already_completed":
                await callback.answer("Инициатива уже принята!", show_alert=False)
            elif reason == "not_found":
                await callback.answer("Инициатива не найдена.", show_alert=True)
            else:
                balance = int(reason.split(":")[1]) if ":" in reason else 0
                await callback.answer(
                    f"Недостаточно монет. У вас: {balance}, нужно: {INITIATIVE_VOTE_COST}.",
                    show_alert=True,
                )
            return

        initiative = result
        new_balance = extra
        await session.commit()

    await callback.answer(f"Поддержали! Ваш остаток: {new_balance} монет", show_alert=False)

    # Обновляем сообщение с прогрессом
    bar_filled = int(initiative.coins_total / initiative.threshold * 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    pct = int(initiative.coins_total / initiative.threshold * 100)

    if just_completed:
        # Инициатива принята — торжественное объявление
        try:
            await callback.message.edit_text(
                f"Инициатива #{initiative.id} ПРИНЯТА!\n\n"
                f"«{initiative.text}»\n\n"
                f"Автор: {initiative.author_name}\n"
                f"Собрано монет: {initiative.coins_total} из {initiative.threshold}\n\n"
                f"Инициатива передана на рассмотрение управляющей компании!",
                reply_markup=None,
            )
        except Exception:
            logger.warning("Не удалось обновить сообщение инициативы #%d", initiative.id)

        # Публичное объявление в чат
        try:
            await bot.send_message(
                settings.forum_chat_id,
                f"ИНИЦИАТИВА ПРИНЯТА!\n\n"
                f"«{initiative.text}»\n\n"
                f"Жители ЖК собрали {initiative.coins_total} монет в поддержку!\n"
                f"Инициатива передана на рассмотрение.",
                message_thread_id=callback.message.message_thread_id,
            )
        except Exception:
            logger.warning("Не удалось отправить объявление о принятии инициативы #%d", initiative.id)
    else:
        # Обновляем прогресс-бар
        try:
            await callback.message.edit_text(
                f"Инициатива #{initiative.id}\n\n"
                f"«{initiative.text}»\n\n"
                f"Автор: {initiative.author_name}\n"
                f"[{bar}] {pct}%\n"
                f"Собрано: {initiative.coins_total} / {initiative.threshold} монет\n\n"
                f"Поддержите инициативу!",
                reply_markup=_initiative_keyboard(initiative.id),
            )
        except Exception:
            pass
