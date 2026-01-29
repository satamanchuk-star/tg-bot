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
    is_question_timed_out,
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


def _display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


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

    async for session in get_session():
        quiz_session = await get_active_session(session, chat_id, topic_id)
        if not quiz_session or not quiz_session.is_active:
            return

        # Проверяем race condition: если вопрос уже сменился, не показываем таймаут
        if quiz_session.question_started_at != started_at_before:
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
                "Викторина завершена!",
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
                "Вопросы закончились. Викторина завершена!",
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


@router.message(Command("umnij"))
async def start_quiz(message: Message, bot: Bot) -> None:
    """Команда /umnij для запуска викторины."""
    if message.chat.id != settings.forum_chat_id:
        return

    if message.message_thread_id != settings.topic_games:
        await message.reply("Эта команда доступна только в топике Игры.")
        return

    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games

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

    # Логируем в админ-чат
    await bot.send_message(
        settings.admin_log_chat_id,
        f"Викторина запущена пользователем {_display_name(message)}",
    )


@router.message(Command("topumnij"))
async def show_quiz_leaderboard(message: Message) -> None:
    """Команда /topumnij для показа рейтинга."""
    if message.chat.id != settings.forum_chat_id:
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
    F.text,
)
async def check_quiz_answer(message: Message, bot: Bot) -> None:
    """Проверяет ответы на вопросы викторины."""
    if message.from_user is None:
        return

    # Пропускаем команды
    if message.text and message.text.startswith("/"):
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
        if not check_answer(question, message.text):
            return

        # Правильный ответ!
        _cancel_timeout(chat_id, topic_id)

        stat = await award_point(
            session,
            message.from_user.id,
            chat_id,
            display_name=_display_name(message),
        )

        name = _display_name(message) or str(message.from_user.id)
        await message.reply(
            f"Правильно, @{name}! +1 очко (всего: {stat.total_points})"
        )

        # Проверяем, закончилась ли викторина
        if await is_quiz_finished(quiz_session):
            await end_quiz_session(session, quiz_session)
            await session.commit()
            await bot.send_message(
                chat_id,
                "Викторина завершена!",
                message_thread_id=topic_id,
            )
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
            _start_timeout(bot, chat_id, topic_id, quiz_session.question_started_at)
        else:
            await end_quiz_session(session, quiz_session)
            await session.commit()
            await bot.send_message(
                chat_id,
                "Вопросы закончились. Викторина завершена!",
                message_thread_id=topic_id,
            )
