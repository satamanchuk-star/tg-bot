"""Почему: админские команды выделены отдельно для контроля доступа."""

from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatPermissions, Message
from sqlalchemy import delete, update

from app.config import settings
from app.db import get_session
from app.models import (
    GameState,
    QuizDailyLimit,
    QuizSession,
    QuizUsedQuestion,
    QuizUserStat,
    UserStat,
)
from app.services.games import can_grant_coins, get_or_create_stats, register_coin_grant
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import extract_target_user, is_admin
from app.utils.admin_help import ADMIN_HELP
from app.handlers.help import clear_routing_state
from app.services.ai_module import get_ai_client, is_ai_runtime_enabled, set_ai_runtime_enabled
from app.services.ai_module import get_ai_runtime_status, get_ai_usage_for_today
from app.services.ai_usage import next_reset_delta, reset_ai_usage
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




@router.message(Command("ai_on"))
async def ai_on(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    set_ai_runtime_enabled(True)
    await message.reply("ИИ-функции включены: модерация, /ai, ответы на упоминания и оценка викторины.")


@router.message(Command("ai_off"))
async def ai_off(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    set_ai_runtime_enabled(False)
    await message.reply("ИИ-функции выключены. Бот перешёл на локальные fallback-правила.")


@router.message(Command("ai_status"))
async def ai_status(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    status = "включены" if is_ai_runtime_enabled() else "выключены"
    req_used, tok_used = await get_ai_usage_for_today(settings.forum_chat_id)
    req_left = max(0, settings.ai_daily_request_limit - req_used)
    tok_left = max(0, settings.ai_daily_token_limit - tok_used)
    reset_in = next_reset_delta()
    runtime = get_ai_runtime_status()
    last_error = runtime.last_error or "нет"
    if runtime.last_error_at:
        last_error = f"{last_error} ({runtime.last_error_at.isoformat(timespec='seconds')} UTC)"

    await message.reply(
        "Статус AI:\n"
        f"• Runtime: {status}\n"
        f"• Endpoint: {settings.ai_api_url or 'не задан'}\n"
        f"• Лимит запросов/сутки: {settings.ai_daily_request_limit} (использовано {req_used}, осталось {req_left})\n"
        f"• Лимит токенов/сутки: {settings.ai_daily_token_limit} (использовано {tok_used}, осталось {tok_left})\n"
        f"• До сброса лимитов: {reset_in}\n"
        f"• Последняя ошибка: {last_error}"
    )


@router.message(Command("ai_ping"))
async def ai_ping(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    result = await get_ai_client().probe()
    status = "✅ AI работает" if result.ok else "❌ AI недоступен"
    await message.reply(f"{status}\nLatency: {result.latency_ms} ms\n{result.details}")


@router.message(Command("ai_reset"))
async def ai_reset(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    async for session in get_session():
        deleted = await reset_ai_usage(session)
    await message.reply(f"AI usage сброшен. Удалено записей: {deleted}.")

@router.message(Command("reload_profanity"))
async def reload_profanity(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    words = load_profanity()
    exceptions = load_profanity_exceptions()
    await message.reply(
        "Словари перечитаны с диска. "
        f"Мат-словарь: {len(words)}, исключения: {len(exceptions)}."
    )


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
    """Загружает вопросы для викторины из XLSX-файла проекта."""
    if not await _ensure_admin(message, bot):
        return

    status_msg = await message.reply("Пересобираю банк вопросов из viktorinavopros_QA.xlsx...")
    from app.services.quiz_loader import sync_questions_from_xlsx

    async for session in get_session():
        total, unique = await sync_questions_from_xlsx(session)
        break

    if total == 0:
        await status_msg.edit_text("Файл с вопросами не найден или пуст.")
        return

    await status_msg.edit_text(
        "Загрузка завершена!\n"
        f"Источник: viktorinavopros_QA.xlsx\n"
        f"Прочитано вопросов: {total}\n"
        f"Уникальных в БД: {unique}"
    )


@router.message(Command("reset_stats"))
async def reset_stats(message: Message, bot: Bot) -> None:
    """Обнуляет статистику игр и викторины, сбрасывая сессию."""
    if not await _ensure_admin(message, bot):
        return

    from app.handlers.quiz import _question_started_at, _session_results, _timeout_tasks

    cleared: list[str] = []

    if _timeout_tasks:
        for task in _timeout_tasks.values():
            task.cancel()
        _timeout_tasks.clear()
        _question_started_at.clear()
        cleared.append("таймауты викторины")

    async for session in get_session():
        game_stats_result = await session.execute(delete(UserStat))
        game_stats_rows = game_stats_result.rowcount or 0
        if game_stats_rows > 0:
            cleared.append(f"статистика игры 21 ({game_stats_rows})")

        game_states_result = await session.execute(delete(GameState))
        game_states_rows = game_states_result.rowcount or 0
        if game_states_rows > 0:
            cleared.append(f"активные игры 21 ({game_states_rows})")

        quiz_stats_result = await session.execute(delete(QuizUserStat))
        quiz_stats_rows = quiz_stats_result.rowcount or 0
        if quiz_stats_rows > 0:
            cleared.append(f"статистика викторины ({quiz_stats_rows})")

        quiz_limits_result = await session.execute(delete(QuizDailyLimit))
        quiz_limits_rows = quiz_limits_result.rowcount or 0
        if quiz_limits_rows > 0:
            cleared.append(f"лимиты запусков викторины ({quiz_limits_rows})")

        used_questions_result = await session.execute(delete(QuizUsedQuestion))
        used_questions_rows = used_questions_result.rowcount or 0
        if used_questions_rows > 0:
            cleared.append(f"глобальная история вопросов ({used_questions_rows})")

        quiz_sessions_result = await session.execute(delete(QuizSession))
        quiz_sessions_rows = quiz_sessions_result.rowcount or 0
        if quiz_sessions_rows > 0:
            cleared.append(f"сессии викторины ({quiz_sessions_rows})")

        await session.commit()

    _session_results.clear()

    if cleared:
        await message.reply("Статистика и сессии сброшены: " + ", ".join(cleared))
    else:
        await message.reply("Статистика уже пустая, сессия сброшена.")


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
            .where(QuizSession.is_active.is_(True))
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
