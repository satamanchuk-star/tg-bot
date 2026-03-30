"""Команды для жителей: поиск услуг, просмотр каталога, продвижение."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.games import get_or_create_stats
from app.services.resident_services import (
    CATEGORY_LABELS,
    list_services_by_category,
    search_services,
)

router = Router()
logger = logging.getLogger(__name__)

# Стоимость продвижения услуги в монетах на 7 дней
PROMO_COST = 200
PROMO_DAYS = 7


@router.message(Command("услуги", "uslugi"))
async def services_catalog_command(message: Message) -> None:
    """Показывает каталог услуг от жителей по категории или выводит все категории."""
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    async for session in get_session():
        if query:
            # Поиск по ключевым словам или категории
            services = await search_services(
                session, settings.forum_chat_id, query, top_k=10
            )
            if not services:
                await message.reply(
                    f"Услуги по запросу «{query}» не найдены.\n"
                    "Попробуйте другое слово или /услуги без аргументов для просмотра всех категорий."
                )
                return
            lines = [f"🔍 Результаты поиска по «{query}»:\n"]
            for svc in services:
                cat = CATEGORY_LABELS.get(svc.category, svc.category)
                promo = " ⭐" if svc.promoted_until and svc.promoted_until > datetime.now(timezone.utc) else ""
                lines.append(f"{cat}{promo} — {svc.description[:150]}")
                if svc.provider_name:
                    lines.append(f"   👤 {svc.provider_name}")
            await message.reply("\n".join(lines))
        else:
            # Показываем все категории со счётчиками
            all_services = await list_services_by_category(
                session, settings.forum_chat_id, limit=200
            )
            if not all_services:
                await message.reply(
                    "Каталог услуг пока пуст.\n"
                    "Напишите о своей услуге в топике «Услуги от жителей»."
                )
                return

            from collections import defaultdict
            by_cat: dict[str, list] = defaultdict(list)
            for svc in all_services:
                by_cat[svc.category].append(svc)

            lines = ["📋 Каталог услуг жителей ЖК:\n"]
            for cat_key, svcs in sorted(by_cat.items(), key=lambda x: -len(x[1])):
                label = CATEGORY_LABELS.get(cat_key, cat_key)
                lines.append(f"{label} — {len(svcs)} услуг(а)")

            lines.append(
                "\nДля поиска: /услуги <запрос>\n"
                "Например: /услуги маникюр или /услуги ремонт"
            )
            await message.reply("\n".join(lines))


@router.message(Command("продвинуть", "promote_service"))
async def promote_service_command(message: Message, bot: Bot) -> None:
    """Продвигает услугу жителя на 7 дней за 200 монет.
    Вызывается реплаем на своё сообщение в топике услуг."""
    if message.from_user is None:
        return

    user_id = message.from_user.id

    if message.reply_to_message is None:
        await message.reply(
            f"Используйте /продвинуть как реплай на ваше сообщение с услугой.\n\n"
            f"Стоимость: {PROMO_COST} монет за {PROMO_DAYS} дней.\n"
            f"Продвинутые услуги отображаются первыми в поиске и помечаются ⭐"
        )
        return

    target_msg = message.reply_to_message

    async for session in get_session():
        # Находим услугу по исходному сообщению
        from app.services.resident_services import get_service_by_source_message_id
        svc = await get_service_by_source_message_id(
            session,
            chat_id=settings.forum_chat_id,
            source_message_id=target_msg.message_id,
        )
        if svc is None:
            await message.reply(
                "Услуга по этому сообщению не найдена в каталоге.\n"
                "Попросите администратора добавить её командой /usluga."
            )
            return

        # Проверяем, что это услуга текущего пользователя
        if svc.provider_user_id != user_id:
            await message.reply("Продвинуть можно только свою услугу.")
            return

        # Проверяем баланс
        stats = await get_or_create_stats(session, user_id, settings.forum_chat_id)
        if stats.coins < PROMO_COST:
            await message.reply(
                f"Недостаточно монет.\n"
                f"Нужно: {PROMO_COST} монет, у вас: {stats.coins}.\n"
                f"Зарабатывайте монеты в играх /21, викторине и рулетке."
            )
            return

        # Если уже продвинута — продлеваем от текущего срока
        now = datetime.now(timezone.utc)
        current_end = svc.promoted_until if svc.promoted_until and svc.promoted_until > now else now
        new_end = current_end + timedelta(days=PROMO_DAYS)

        svc.promoted_until = new_end
        stats.coins -= PROMO_COST
        await session.commit()

    cat = CATEGORY_LABELS.get(svc.category, svc.category)
    await message.reply(
        f"⭐ Услуга продвинута!\n"
        f"Категория: {cat}\n"
        f"Описание: {svc.description[:150]}\n\n"
        f"Продвижение активно до: {new_end.strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"Списано: {PROMO_COST} монет\n"
        f"Остаток: {stats.coins} монет"
    )
    logger.info(
        "PROMOTE: пользователь %s продвинул услугу %s до %s",
        user_id, svc.id, new_end,
    )


@router.message(Command("передать", "transfer_coins"))
async def transfer_coins_command(message: Message) -> None:
    """Передаёт монеты другому жителю. Формат: /передать @username 50"""
    if message.from_user is None:
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(
            "Формат: /передать @username <количество>\n"
            "Например: /передать @Иван 50\n\n"
            "Минимальная передача: 10 монет."
        )
        return

    amount_str = parts[-1]
    try:
        amount = int(amount_str)
    except ValueError:
        await message.reply("Укажите число монет. Например: /передать @Иван 50")
        return

    if amount < 10:
        await message.reply("Минимальная передача — 10 монет.")
        return

    # Получатель: из реплая или из упоминания
    target_user_id: int | None = None
    target_name: str | None = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    else:
        # Ищем mention в entities
        if message.entities:
            for entity in message.entities:
                if entity.type == "text_mention" and entity.user:
                    target_user_id = entity.user.id
                    target_name = entity.user.full_name
                    break
        if target_user_id is None:
            await message.reply(
                "Не могу определить получателя.\n"
                "Используйте /передать как реплай на сообщение получателя или упоминайте его через @."
            )
            return

    sender_id = message.from_user.id
    if target_user_id == sender_id:
        await message.reply("Нельзя передавать монеты самому себе.")
        return

    async for session in get_session():
        sender_stats = await get_or_create_stats(session, sender_id, settings.forum_chat_id)
        if sender_stats.coins < amount:
            await message.reply(
                f"Недостаточно монет.\n"
                f"Нужно: {amount}, у вас: {sender_stats.coins}."
            )
            return

        receiver_stats = await get_or_create_stats(session, target_user_id, settings.forum_chat_id)

        sender_stats.coins -= amount
        receiver_stats.coins += amount
        await session.commit()

    sender_name = message.from_user.full_name
    recipient_display = target_name or str(target_user_id)

    await message.reply(
        f"✅ Передано {amount} монет!\n"
        f"От: {sender_name}\n"
        f"Кому: {recipient_display}\n"
        f"Ваш остаток: {sender_stats.coins} монет"
    )
    logger.info(
        "TRANSFER: %s → %s, %d монет",
        sender_id, target_user_id, amount,
    )
