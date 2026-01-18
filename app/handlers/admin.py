"""Почему: админские команды выделены отдельно для контроля доступа."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import ChatPermissions, Message

from app.config import settings
from app.db import get_session
from app.services.games import can_grant_coins, get_or_create_stats, register_coin_grant
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import extract_target_user, is_admin
from app.handlers.moderation import update_profanity
from app.utils.profanity import load_profanity

router = Router()


ADMIN_HELP = (
    "Админ-команды:\n"
    "/mute <минуты> (реплай или id)\n"
    "/unmute (реплай или id)\n"
    "/ban <дни> (реплай или id)\n"
    "/unban (реплай или id)\n"
    "/strike (реплай или id)\n"
    "/coins <кол-во> (реплай или id, не более 10 за раз)\n"
    "/reload_profanity"
)


@router.message(Command("admin"))
async def admin_help(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    await message.reply(ADMIN_HELP)


@router.message(Command("mute"))
async def mute_user(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество минут.")
        return
    try:
        minutes = int(parts[1])
    except ValueError:
        await message.reply("Минуты должны быть числом.")
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай или id пользователя.")
        return
    until = datetime.utcnow() + timedelta(minutes=minutes)
    permissions = ChatPermissions(can_send_messages=False)
    await bot.restrict_chat_member(
        settings.forum_chat_id,
        target_id,
        permissions=permissions,
        until_date=until,
    )
    await message.reply(f"Пользователь замьючен на {minutes} минут.")


@router.message(Command("unmute"))
async def unmute_user(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай или id пользователя.")
        return
    permissions = ChatPermissions(can_send_messages=True, can_send_other_messages=True)
    await bot.restrict_chat_member(
        settings.forum_chat_id, target_id, permissions=permissions
    )
    await message.reply("Мут снят.")


@router.message(Command("ban"))
async def ban_user(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество дней.")
        return
    try:
        days = int(parts[1])
    except ValueError:
        await message.reply("Дни должны быть числом.")
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай или id пользователя.")
        return
    until = datetime.utcnow() + timedelta(days=days)
    await bot.ban_chat_member(settings.forum_chat_id, target_id, until_date=until)
    await message.reply(f"Бан на {days} дней выдан.")


@router.message(Command("unban"))
async def unban_user(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай или id пользователя.")
        return
    await bot.unban_chat_member(settings.forum_chat_id, target_id)
    await message.reply("Бан снят.")


@router.message(Command("strike"))
async def strike_user(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай или id пользователя.")
        return
    async for session in get_session():
        count = await add_strike(session, target_id, settings.forum_chat_id)
        await session.commit()
    if count >= 3:
        until = datetime.utcnow() + timedelta(hours=24)
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            settings.forum_chat_id,
            target_id,
            permissions=permissions,
            until_date=until,
        )
        async for session in get_session():
            await clear_strikes(session, target_id, settings.forum_chat_id)
            await session.commit()
        await message.reply("Третий страйк! Мут на 24 часа.")
        return
    await message.reply(f"Страйк добавлен. Всего: {count}")


@router.message(Command("coins"))
async def grant_coins(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество монет.")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply("Монеты должны быть числом.")
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай или id пользователя.")
        return
    async for session in get_session():
        stats = await get_or_create_stats(
            session,
            target_id,
            settings.forum_chat_id,
            display_name=display_name,
        )
        now = datetime.utcnow()
        if not can_grant_coins(stats, now, amount):
            await message.reply("Нельзя выдать больше 10 монет за раз/сутки.")
            return
        register_coin_grant(stats, now, amount)
        await session.commit()
    await message.reply(f"Начислено {amount} монет.")


@router.message(Command("reload_profanity"))
async def reload_profanity(message: Message, bot: Bot) -> None:
    if not await is_admin(bot, settings.forum_chat_id, message.from_user.id):
        return
    words = load_profanity()
    update_profanity(words)
    await message.reply(f"Список матов обновлен. Слов: {len(words)}")
