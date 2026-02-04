"""Почему: хендлеры викторины изолированы от остальных игр."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.utils.admin import is_admin_message
from app.services.quiz import (
    QUIZ_QUESTION_TIMEOUT_SEC,
    QUIZ_QUESTIONS_COUNT,
    award_point,
    can_start_quiz,
    check_answer,
    end_quiz_session,
    get_active_session,
    get_current_question,
    get_quiz_leaderboard,
    get_random_question,
    is_quiz_finished,
    set_current_question,
    start_quiz_session,
)

if TYPE_CHECKING:
    from app.models import QuizSession

logger = logging.getLogger(__name__)

router = Router()

# Хранилище активных таймаутов (chat_id, topic_id) -> asyncio.Task
_timeout_tasks: dict[tuple[int, int], asyncio.Task] = {}

# Хранилище времени начала вопроса для проверки race condition
_question_started_at: dict[tuple[int, int], datetime | None] = {}

# Результаты текущей сессии: (chat_id, topic_id) -> {user_id: (display_name, points)}
_session_results: dict[tuple[int, int], dict[int, tuple[str, int]]] = {}


def _pluralize_points(n: int) -> str:
    """Склонение слова 'очко' в зависимости от числа."""
    if 11 <= n % 100 <= 14:
        return "очков"
    if n % 10 == 1:
        return "очко"
    if 2 <= n % 10 <= 4:
        return "очка"
    return "очков"


def _format_results(chat_id: int, topic_id: int) -> str:
    """Форматирует итоги викторины и очищает результаты сессии."""
    key = (chat_id, topic_id)
    results = _session_results.pop(key, {})
    if not results:
        return "Викторина завершена! Никто не ответил правильно."

    lines = ["Викторина завершена!\n\nИтоги:"]
    sorted_results = sorted(results.items(), key=lambda x: -x[1][1])
    for user_id, (name, points) in sorted_results:
        lines.append(f"• @{name}: +{points} {_pluralize_points(points)}")
    return "\n".join(lines)


def _display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


def _display_name_from_user(user: object) -> str | None:
    if not hasattr(user, "username") or not hasattr(user, "full_name"):
        return None
    return user.username or user.full_name


async def announce_quiz_soon(bot: Bot) -> None:
    """Анонсирует старт викторины за 5 минут."""
    if settings.topic_games is None:
        logger.info("Анонс викторины пропущен: topic_games не задан.")
        return
    await bot.send_message(
        settings.forum_chat_id,
        "уважаемые соседи через 5 минут начнется викторина в топике "
        "Блэкджек и боулинг, приходите размять мозг",
    )
    await bot.send_message(
        settings.forum_chat_id,
        "Привет соседи, давайте поиграем! через 5 минут начнем!",
        message_thread_id=settings.topic_games,
    )


async def start_quiz_auto(bot: Bot) -> None:
    """Автоматически запускает викторину."""
    if settings.topic_games is None:
        logger.info("Авто-викторина пропущена: topic_games не задан.")
        return
    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games

    _session_results[(chat_id, topic_id)] = {}
    question = None
    question_started_at = None

    async for session in get_session():
        can_start, reason = await can_start_quiz(session, chat_id, topic_id)
        if not can_start:
            logger.info("Авто-викторина не запущена: %s", reason)
            await bot.send_message(
                settings.admin_log_chat_id,
                f"Авто-викторина не запущена: {reason}",
            )
            return

        quiz_session = await start_quiz_session(session, chat_id, topic_id)
        question = await get_random_question(session, quiz_session)
        if not question:
            await bot.send_message(chat_id, "Нет доступных вопросов.", message_thread_id=topic_id)
            return

        await set_current_question(session, quiz_session, question)
        await session.commit()
        question_started_at = quiz_session.question_started_at

    if question is None:
        return

    await bot.send_message(
        chat_id,
        "Викторина начинается! У вас 60 секунд на каждый вопрос.",
        message_thread_id=topic_id,
    )
    await _send_question(bot, chat_id, topic_id, 1, question.question)
    _start_timeout(bot, chat_id, topic_id, question_started_at)

    await bot.send_message(
        settings.admin_log_chat_id,
        "Викторина запущена автоматически",
    )


async def _send_question(bot: Bot, chat_id: int, topic_id: int, question_num: int, question_text: str) -> None:
    """Отправляет вопрос в чат."""
    await bot.send_message(
        chat_id,
        f"Вопрос {question_num}/{QUIZ_QUESTIONS_COUNT}:\n\n{question_text}",
        message_thread_id=topic_id,
    )


async def _handle_timeout(bot: Bot, chat_id: int, topic_id: int) -> None:
    """Обрабатывает таймаут вопроса."""
    # Запоминаем время начала вопроса ДО сна
    key = (chat_id, topic_id)
    started_at_before = _question_started_at.get(key)

    await asyncio.sleep(QUIZ_QUESTION_TIMEOUT_SEC)

    # Проверяем race condition: если значение в dict изменилось, вопрос был отвечен
    current_started_at = _question_started_at.get(key)
    if current_started_at != started_at_before:
        return

    async for session in get_session():
        quiz_session = await get_active_session(session, chat_id, topic_id)
        if not quiz_session or not quiz_session.is_active:
            return

        question = await get_current_question(session, quiz_session)
        if question:
            await bot.send_message(
                chat_id,
                f"Время вышло! Правильный ответ: {question.answer}",
                message_thread_id=topic_id,
            )

        # Проверяем, закончилась ли викторина
        if await is_quiz_finished(quiz_session):
            await end_quiz_session(session, quiz_session)
            await session.commit()
            await bot.send_message(
                chat_id,
                _format_results(chat_id, topic_id),
                message_thread_id=topic_id,
            )
            _cancel_timeout(chat_id, topic_id)
            return

        # Следующий вопрос
        next_question = await get_random_question(session, quiz_session)
        if next_question:
            await set_current_question(session, quiz_session, next_question)
            await session.commit()
            await _send_question(
                bot, chat_id, topic_id,
                quiz_session.question_number, next_question.question
            )
            # Запускаем новый таймаут
            _start_timeout(bot, chat_id, topic_id, quiz_session.question_started_at)
        else:
            await end_quiz_session(session, quiz_session)
            await session.commit()
            await bot.send_message(
                chat_id,
                _format_results(chat_id, topic_id),
                message_thread_id=topic_id,
            )
            _cancel_timeout(chat_id, topic_id)


def _start_timeout(bot: Bot, chat_id: int, topic_id: int, question_started_at: datetime | None) -> None:
    """Запускает таймаут для текущего вопроса."""
    _cancel_timeout(chat_id, topic_id)
    key = (chat_id, topic_id)
    _question_started_at[key] = question_started_at
    task = asyncio.create_task(_handle_timeout(bot, chat_id, topic_id))
    _timeout_tasks[key] = task


def _cancel_timeout(chat_id: int, topic_id: int) -> None:
    """Отменяет активный таймаут."""
    key = (chat_id, topic_id)
    if key in _timeout_tasks:
        _timeout_tasks[key].cancel()
        del _timeout_tasks[key]


async def _start_quiz_from_message(
    message: Message,
    bot: Bot,
    *,
    log_prefix: str,
) -> None:
    """Запускает викторину из командного сообщения с проверкой топика."""
    if message.chat.id != settings.forum_chat_id:
        return
    if settings.topic_games is None:
        return
    if message.message_thread_id != settings.topic_games:
        await message.reply("Эта команда доступна только в топике Игры.")
        return

    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games

    _session_results[(chat_id, topic_id)] = {}

    async for session in get_session():
        can_start, reason = await can_start_quiz(session, chat_id, topic_id)
        if not can_start:
            await message.reply(reason)
            return

        quiz_session = await start_quiz_session(session, chat_id, topic_id)

        question = await get_random_question(session, quiz_session)
        if not question:
            await message.reply("Нет доступных вопросов.")
            return

        await set_current_question(session, quiz_session, question)
        await session.commit()
        question_started_at = quiz_session.question_started_at

    await message.reply("Викторина начинается! У вас 60 секунд на каждый вопрос.")
    await _send_question(bot, chat_id, topic_id, 1, question.question)
    _start_timeout(bot, chat_id, topic_id, question_started_at)

    await bot.send_message(
        settings.admin_log_chat_id,
        f"{log_prefix} {_display_name(message)}",
    )


@router.message(Command("umnij"))
async def start_quiz(message: Message, bot: Bot) -> None:
    """Команда /umnij для запуска викторины."""
    await _start_quiz_from_message(
        message,
        bot,
        log_prefix="Викторина запущена пользователем",
    )


@router.message(Command("umnij_start"))
async def start_quiz_admin(message: Message, bot: Bot) -> None:
    """Админская команда /umnij_start для ручного запуска викторины."""
    if not await is_admin_message(bot, settings.forum_chat_id, message):
        return
    await _start_quiz_from_message(
        message,
        bot,
        log_prefix="Викторина запущена админом",
    )


@router.message(Command("bal"))
async def add_quiz_point_admin(message: Message, bot: Bot) -> None:
    """Админская команда для добавления +1 балла в викторине."""
    if settings.topic_games is None:
        return
    if not await is_admin_message(bot, settings.forum_chat_id, message):
        return
    if (
        message.chat.id != settings.forum_chat_id
        or message.message_thread_id != settings.topic_games
    ):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Нужен реплай на сообщение игрока.")
        return

    target_user = message.reply_to_message.from_user
    display_name = _display_name_from_user(target_user) or str(target_user.id)

    async for session in get_session():
        quiz_session = await get_active_session(
            session, settings.forum_chat_id, settings.topic_games
        )
        if not quiz_session:
            await message.reply("Сейчас викторина не запущена.")
            return

        stat = await award_point(
            session,
            target_user.id,
            settings.forum_chat_id,
            display_name=display_name,
        )
        await session.commit()

    key = (settings.forum_chat_id, settings.topic_games)
    if key not in _session_results:
        _session_results[key] = {}
    results = _session_results[key]
    if target_user.id in results:
        results[target_user.id] = (display_name, results[target_user.id][1] + 1)
    else:
        results[target_user.id] = (display_name, 1)

    await message.reply(f"Добавлен 1 балл @{display_name}. Всего: {stat.total_points}")


@router.message(Command("topumnij"))
async def show_quiz_leaderboard(message: Message) -> None:
    """Команда /topumnij для показа рейтинга."""
    if message.chat.id != settings.forum_chat_id:
        return
    if settings.topic_games is None:
        return

    if message.message_thread_id != settings.topic_games:
        await message.reply("Эта команда доступна только в топике Игры.")
        return

    async for session in get_session():
        top_players = await get_quiz_leaderboard(session, settings.forum_chat_id)

    if not top_players:
        await message.reply("Рейтинг пуст. Сыграйте в викторину!")
        return

    lines = ["Топ-5 умников:"]
    for i, stat in enumerate(top_players, 1):
        name = stat.display_name or str(stat.user_id)
        lines.append(f"{i}. @{name} — {stat.total_points} очков")

    await message.reply("\n".join(lines))


@router.message(
    F.chat.id == settings.forum_chat_id,
    F.message_thread_id == settings.topic_games,
    (F.text | F.caption),
)
async def check_quiz_answer(message: Message, bot: Bot) -> None:
    """Проверяет ответы на вопросы викторины."""
    if settings.topic_games is None:
        return

    # Пропускаем команды
    message_text = message.text or message.caption
    if not message_text:
        return
    if message_text.startswith("/"):
        return
    if message.from_user is None:
        return

    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games

    async for session in get_session():
        quiz_session = await get_active_session(session, chat_id, topic_id)
        if not quiz_session:
            return

        question = await get_current_question(session, quiz_session)
        if not question:
            return

        # Проверяем ответ
        if not check_answer(question, message_text):
            return

        # Правильный ответ!
        _cancel_timeout(chat_id, topic_id)
        # Сбрасываем таймстамп для защиты от race condition
        _question_started_at[(chat_id, topic_id)] = None

        stat = await award_point(
            session,
            message.from_user.id,
            chat_id,
            display_name=_display_name(message),
        )

        name = _display_name(message) or str(message.from_user.id)

        # Отслеживаем результаты сессии
        key = (chat_id, topic_id)
        if key not in _session_results:
            _session_results[key] = {}
        results = _session_results[key]
        user_id = message.from_user.id
        if user_id in results:
            results[user_id] = (name, results[user_id][1] + 1)
        else:
            results[user_id] = (name, 1)
        await message.reply(
            f"Правильно, @{name}! +1 очко (всего: {stat.total_points})"
        )

        # Проверяем, закончилась ли викторина
        if await is_quiz_finished(quiz_session):
            await end_quiz_session(session, quiz_session)
            await session.commit()
            await bot.send_message(
                chat_id,
                _format_results(chat_id, topic_id),
                message_thread_id=topic_id,
            )
            return

        # Следующий вопрос
        quiz_session.current_question_id = None
        quiz_session.question_started_at = None
        await session.commit()
        await asyncio.sleep(60)
        next_question = await get_random_question(session, quiz_session)
        if next_question:
            await set_current_question(session, quiz_session, next_question)
            await session.commit()
            await _send_question(
                bot, chat_id, topic_id,
                quiz_session.question_number, next_question.question
            )
            _start_timeout(bot, chat_id, topic_id, quiz_session.question_started_at)
        else:
            await end_quiz_session(session, quiz_session)
            await session.commit()
            await bot.send_message(
                chat_id,
                _format_results(chat_id, topic_id),
                message_thread_id=topic_id,
            )
