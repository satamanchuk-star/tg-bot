"""Почему: админские команды выделены отдельно для контроля доступа."""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatPermissions, Message
from sqlalchemy import delete, update

from app.config import settings
from app.db import get_session
from app.models import GameState, QuizSession
from app.services.games import can_grant_coins, get_or_create_stats, register_coin_grant
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import extract_target_user, is_admin
from app.utils.admin_help import ADMIN_HELP
from app.handlers.moderation import update_profanity, update_profanity_exceptions
from app.handlers.help import clear_routing_state
from app.utils.profanity import load_profanity, load_profanity_exceptions

router = Router()
logger = logging.getLogger(__name__)


STOP_FLAG = settings.data_dir / ".stopped"


def _admin_label(message: Message) -> str:
    if message.from_user:
        return message.from_user.full_name
    if message.sender_chat:
        return message.sender_chat.title or str(message.sender_chat.id)
    return "неизвестный админ"


def _admin_id(message: Message) -> str:
    if message.from_user:
        return str(message.from_user.id)
    if message.sender_chat:
        return str(message.sender_chat.id)
    return "unknown"


async def _ensure_admin(message: Message, bot: Bot) -> bool:
    if message.from_user is None:
        if message.sender_chat and message.sender_chat.id == settings.forum_chat_id:
            return True
        return False
    try:
        return await is_admin(bot, settings.forum_chat_id, message.from_user.id)
    except Exception:  # noqa: BLE001 - не выдаём доступ при ошибке проверки
        logger.exception("Не удалось проверить права администратора.")
        return False


@router.message(Command("admin"))
async def admin_help(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        if message.sender_chat:
            await message.reply(ADMIN_HELP)
        return
    if not await _ensure_admin(message, bot):
        return
    await message.reply(ADMIN_HELP)


@router.message(Command("mute"))
async def mute_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
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
        await message.reply("Нужен реплай на сообщение пользователя.")
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
    if not await _ensure_admin(message, bot):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    permissions = ChatPermissions(can_send_messages=True, can_send_other_messages=True)
    await bot.restrict_chat_member(
        settings.forum_chat_id, target_id, permissions=permissions
    )
    await message.reply("Мут снят.")


@router.message(Command("ban"))
async def ban_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
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
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    until = datetime.utcnow() + timedelta(days=days)
    await bot.ban_chat_member(settings.forum_chat_id, target_id, until_date=until)
    await message.reply(f"Бан на {days} дней выдан.")


@router.message(Command("unban"))
async def unban_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    await bot.unban_chat_member(settings.forum_chat_id, target_id)
    await message.reply("Бан снят.")


@router.message(Command("strike"))
async def strike_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
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


@router.message(Command("addcoins"))
async def grant_coins(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
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
        await message.reply("Нужен реплай на сообщение пользователя.")
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
    if not await _ensure_admin(message, bot):
        return
    words = load_profanity()
    exceptions = load_profanity_exceptions()
    update_profanity(words)
    update_profanity_exceptions(exceptions)
    await message.reply(f"Список матов обновлен. Слов: {len(words)}")


@router.message(Command("reset_routing_state"))
async def reset_routing_state(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return

    target_id, display_name = extract_target_user(message)
    parts = (message.text or "").split(maxsplit=1)
    if target_id is None and len(parts) > 1:
        raw_target = parts[1].strip()
        if raw_target.startswith("@"):
            try:
                chat = await bot.get_chat(raw_target)
            except Exception:  # noqa: BLE001 - Telegram API может ответить ошибкой
                chat = None
            target_id = chat.id if chat else None
            display_name = raw_target
        elif raw_target.isdigit():
            target_id = int(raw_target)
            display_name = raw_target

    if target_id is None:
        cleared = clear_routing_state()
        await message.reply(f"Сброшено ожиданий: {cleared}.")
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Админ {_admin_id(message)} сбросил все ожидания /help.",
        )
        return

    cleared = clear_routing_state(user_id=target_id, chat_id=settings.forum_chat_id)
    await message.reply(
        f"Ожидание для пользователя {display_name or target_id} сброшено."
    )
    if cleared:
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Админ {_admin_id(message)} сбросил ожидание /help для {target_id}.",
        )


@router.message(Command("load_quiz"))
async def load_quiz_questions(message: Message, bot: Bot) -> None:
    """Загружает вопросы для викторины из внешних источников."""
    if not await _ensure_admin(message, bot):
        return

    from app.services.quiz_loader import (
        load_questions_from_xlsx,
        save_questions_to_db,
    )

    status_msg = await message.reply("Начинаю загрузку вопросов...")
    questions: list[tuple[str, str]] = []
    source_stats: list[tuple[str, int]] = []

    async def collect_with_progress(
        loader: AsyncGenerator[str, None],
        prefix: str,
    ) -> list[tuple[str, str]]:
        collected: list[tuple[str, str]] = []
        last_update = ""
        async for progress in loader:
            if progress.startswith("DONE"):
                parts = progress.split("|")
                if len(parts) > 1:
                    for i in range(1, len(parts) - 1, 2):
                        collected.append((parts[i], parts[i + 1]))
            else:
                if progress != last_update:
                    last_update = progress
                    try:
                        await status_msg.edit_text(f"{prefix}: {progress}")
                    except Exception:
                        pass
        return collected

    sources = [
        ("viktorinavopros_QA.xlsx", load_questions_from_xlsx),
    ]

    for source_name, loader_factory in sources:
        source_questions = await collect_with_progress(
            loader_factory(),
            source_name,
        )
        source_stats.append((source_name, len(source_questions)))
        questions.extend(source_questions)

    if not questions:
        await status_msg.edit_text("Вопросы не найдены ни в одном источнике.")
        return

    # Сохраняем в БД
    async for session in get_session():
        added = await save_questions_to_db(session, questions)

    details = "\n".join(f"• {name}: найдено {count}" for name, count in source_stats)
    await status_msg.edit_text(
        f"Загрузка завершена!\n"
        f"Найдено вопросов: {len(questions)}\n"
        f"Добавлено новых: {added}\n"
        f"{details}"
    )


@router.message(Command("restart_jobs"))
async def restart_jobs(message: Message, bot: Bot, state: FSMContext) -> None:
    """Останавливает все зависшие задачи (формы, квизы, игры)."""
    if not await _ensure_admin(message, bot):
        return

    cleared = []

    # 1. Отменяем таймауты квиза
    from app.handlers.quiz import _timeout_tasks

    if _timeout_tasks:
        for task in _timeout_tasks.values():
            task.cancel()
        _timeout_tasks.clear()
        cleared.append("таймауты квиза")

    # 2. Очищаем БД
    async for session in get_session():
        # Игры
        result = await session.execute(delete(GameState))
        if result.rowcount > 0:
            cleared.append(f"игры ({result.rowcount})")

        # Квизы
        result = await session.execute(
            update(QuizSession)
            .where(QuizSession.is_active == True)
            .values(is_active=False)
        )
        if result.rowcount > 0:
            cleared.append(f"квизы ({result.rowcount})")

        await session.commit()

    # 3. Очищаем FSM (через storage)
    storage = state.storage
    # MemoryStorage хранит данные в _data dict
    if hasattr(storage, "_data"):
        storage._data.clear()
        cleared.append("FSM-состояния")

    if cleared:
        await message.reply(f"Очищено: {', '.join(cleared)}")
    else:
        await message.reply("Нет зависших задач.")


@router.message(Command("shutdown_bot"))
async def shutdown_bot_cmd(message: Message, bot: Bot) -> None:
    """Полностью останавливает бота без автоматического перезапуска."""
    if not await _ensure_admin(message, bot):
        return

    # Создаём файл-флаг для предотвращения перезапуска
    STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.touch()

    await message.reply("🛑 Бот останавливается...")
    await bot.send_message(
        settings.admin_log_chat_id,
        f"🛑 Бот остановлен командой /shutdown_bot\n"
        f"Админ: {_admin_label(message)}\n"
        f"Для запуска: удалить {STOP_FLAG} и перезапустить контейнер",
    )

    # Отправляем сигнал завершения процессу
    os.kill(os.getpid(), signal.SIGTERM)
