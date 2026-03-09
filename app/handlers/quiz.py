"""Почему: оркеструем жизненный цикл викторины (таймеры, уведомления, команды) в одном месте."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from itertools import cycle
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.quiz import (
    QUIZ_BREAK_BETWEEN_QUESTIONS_SEC,
    QUIZ_CORRECT_ANSWER_COINS,
    QUIZ_QUESTION_TIMEOUT_SEC,
    QUIZ_QUESTIONS_COUNT,
    QUIZ_WINNER_COINS_BONUS,
    award_correct_answer_coins,
    award_point,
    award_winner_bonus_coins,
    build_answer_hint,
    build_session_stats,
    can_start_quiz,
    end_quiz_session,
    get_active_session,
    get_current_question,
    get_questions_left,
    get_quiz_leaderboard,
    get_random_question,
    is_quiz_finished,
    set_current_question,
    start_quiz_session,
    winners_from_results,
)
from app.services.ai_module import get_ai_client
from app.utils.admin import is_admin_message

if TYPE_CHECKING:
    from app.models import QuizSession

logger = logging.getLogger(__name__)
router = Router()

_timeout_tasks: dict[tuple[int, int], asyncio.Task] = {}
_question_started_at: dict[tuple[int, int], datetime | None] = {}
_session_results: dict[tuple[int, int], dict[int, tuple[str, int]]] = {}
_answer_grace_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
_pending_answers: dict[tuple[int, int], dict[int, tuple[str, bool]]] = {}
_session_locks: dict[tuple[int, int], asyncio.Lock] = {}

_MAIN_CHAT_INVITES = cycle(
    [
        "Дорогие соседи, в топике «Блэкджек и боулинг» через 5 минут стартует викторина!",
        "Соседи, собираемся в «Блэкджек и боулинг»: через 5 минут начинаем викторину.",
        "Внимание, соседи! Через 5 минут в игровом топике начнётся викторина.",
        "Кто сегодня самый умный сосед? Узнаем через 5 минут в «Блэкджек и боулинг».",
        "Подтягивайтесь в «Блэкджек и боулинг»: до викторины осталось 5 минут.",
    ]
)

_TOPIC_INVITES = cycle(
    [
        "Эй, соседи, через 5 минут викторина — приходите выяснить, кто не дружит со шлагбаумом 😄",
        "Через 5 минут начинаем! Ждём героев и тех, кто опять забудет, как работает шлагбаум 😅",
        "Соседи, разминка для мозгов через 5 минут. Проверим, кто у нас чемпион по странным ответам 😎",
        "Через 5 минут викторина! Заходите доказать, что шлагбаум вам всё-таки по силам 🤓",
        "Пять минут до старта викторины — самое время блеснуть умом (или хотя бы попытаться) 😜",
    ]
)


def _display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


async def announce_quiz_soon(bot: Bot) -> None:
    if settings.topic_games is None:
        return
    await bot.send_message(settings.forum_chat_id, next(_MAIN_CHAT_INVITES))
    await bot.send_message(
        settings.forum_chat_id,
        next(_TOPIC_INVITES),
        message_thread_id=settings.topic_games,
    )


async def announce_quiz_rules(bot: Bot) -> None:
    """Публикует правила викторины за минуту до старта."""
    if settings.topic_games is None:
        return
    await bot.send_message(
        settings.forum_chat_id,
        "📋 Правила викторины\n\n"
        f"• {QUIZ_QUESTIONS_COUNT} вопросов подряд\n"
        f"• {QUIZ_QUESTION_TIMEOUT_SEC} секунд на каждый ответ\n"
        f"• Пауза {QUIZ_BREAK_BETWEEN_QUESTIONS_SEC} секунд между вопросами\n"
        f"• За правильный ответ: +{QUIZ_CORRECT_ANSWER_COINS} монет\n"
        f"• Победитель раунда получает бонус: +{QUIZ_WINNER_COINS_BONUS} монет\n"
        "• Каждый может ответить на вопрос только 1 раз\n"
        "• Пишите ответ обычным сообщением в чат\n"
        "• Бот оценивает ответы с учётом опечаток\n\n"
        "Викторина стартует через 1 минуту. Удачи! 🍀",
        message_thread_id=settings.topic_games,
    )


async def announce_questions_left(bot: Bot) -> None:
    if settings.topic_games is None:
        return
    async for session in get_session():
        left = await get_questions_left(session)
        break
    else:
        return
    await bot.send_message(
        settings.forum_chat_id,
        f"До старта викторины 1 минута. В базе осталось вопросов: {left}.",
        message_thread_id=settings.topic_games,
    )


async def start_quiz_auto(bot: Bot) -> None:
    if settings.topic_games is None:
        return
    await _start_quiz(bot, settings.forum_chat_id, settings.topic_games, actor="авто")


async def _start_quiz(bot: Bot, chat_id: int, topic_id: int, actor: str) -> tuple[bool, str]:
    _session_results[(chat_id, topic_id)] = {}
    _cancel_answer_grace(chat_id, topic_id)

    async for session in get_session():
        can_start, reason = await can_start_quiz(session, chat_id, topic_id)
        if not can_start:
            if actor == "авто":
                await bot.send_message(settings.admin_log_chat_id, f"Автозапуск викторины отменён: {reason}")
            return False, reason

        quiz_session = await start_quiz_session(session, chat_id, topic_id)
        question = await get_random_question(session, quiz_session)
        if not question:
            await bot.send_message(chat_id, "Вопросы закончились. Загрузите новую базу.", message_thread_id=topic_id)
            return False, "Вопросы закончились. Загрузите новую базу."

        await set_current_question(session, quiz_session, question)
        await session.commit()
        question_started_at = quiz_session.question_started_at
        question_number = quiz_session.question_number
        question_text = question.question
        hint = build_answer_hint(question.answer)
        break

    await bot.send_message(
        chat_id,
        f"Викторина начинается! {QUIZ_QUESTIONS_COUNT} вопросов, по 60 секунд на ответ и 60 секунд пауза между вопросами.\n"
        f"Каждый пользователь может ответить на каждый вопрос только 1 раз.\n"
        f"За правильный ответ: +{QUIZ_CORRECT_ANSWER_COINS} монет.",
        message_thread_id=topic_id,
    )
    await _send_question(bot, chat_id, topic_id, question_number, question_text, hint)
    _start_timeout(bot, chat_id, topic_id, question_started_at)
    return True, ""


async def _send_question(
    bot: Bot,
    chat_id: int,
    topic_id: int,
    question_num: int,
    question_text: str,
    answer_hint: str,
) -> None:
    await bot.send_message(
        chat_id,
        f"Вопрос {question_num}/{QUIZ_QUESTIONS_COUNT}:\n\n{question_text}\n\n{answer_hint}",
        message_thread_id=topic_id,
    )


def _start_timeout(bot: Bot, chat_id: int, topic_id: int, question_started_at: datetime | None) -> None:
    _cancel_timeout(chat_id, topic_id)
    key = (chat_id, topic_id)
    _question_started_at[key] = question_started_at
    _timeout_tasks[key] = asyncio.create_task(_handle_timeout(bot, chat_id, topic_id))


def _cancel_timeout(chat_id: int, topic_id: int) -> None:
    task = _timeout_tasks.pop((chat_id, topic_id), None)
    if task:
        task.cancel()


def _get_session_lock(chat_id: int, topic_id: int) -> asyncio.Lock:
    key = (chat_id, topic_id)
    if key not in _session_locks:
        _session_locks[key] = asyncio.Lock()
    return _session_locks[key]


def _cancel_answer_grace(chat_id: int, topic_id: int) -> None:
    key = (chat_id, topic_id)
    task = _answer_grace_tasks.pop(key, None)
    if task:
        task.cancel()
    _pending_answers.pop(key, None)


async def _handle_timeout(bot: Bot, chat_id: int, topic_id: int) -> None:
    key = (chat_id, topic_id)
    started_before = _question_started_at.get(key)
    await asyncio.sleep(QUIZ_QUESTION_TIMEOUT_SEC)
    if _question_started_at.get(key) != started_before:
        return

    async for session in get_session():
        quiz_session = await get_active_session(session, chat_id, topic_id)
        if not quiz_session:
            return

        question = await get_current_question(session, quiz_session)
        if question:
            await bot.send_message(
                chat_id,
                f"Время вышло! Правильный ответ: {question.answer}",
                message_thread_id=topic_id,
            )

        quiz_session.current_question_id = None
        quiz_session.question_started_at = None
        await session.commit()

        if await is_quiz_finished(quiz_session):
            await _finish_quiz_and_notify(session, bot, chat_id, topic_id, quiz_session)
            _cancel_timeout(chat_id, topic_id)
            return
        break

    await _send_next_question_after_break(bot, chat_id, topic_id)


async def _send_next_question_after_break(bot: Bot, chat_id: int, topic_id: int) -> None:
    await asyncio.sleep(QUIZ_BREAK_BETWEEN_QUESTIONS_SEC)
    async for session in get_session():
        quiz_session = await get_active_session(session, chat_id, topic_id)
        if not quiz_session:
            return

        question = await get_random_question(session, quiz_session)
        if not question:
            await _finish_quiz_and_notify(session, bot, chat_id, topic_id, quiz_session)
            _cancel_timeout(chat_id, topic_id)
            return

        await set_current_question(session, quiz_session, question)
        await session.commit()
        question_started_at = quiz_session.question_started_at
        question_number = quiz_session.question_number
        question_text = question.question
        hint = build_answer_hint(question.answer)
        break

    await _send_question(bot, chat_id, topic_id, question_number, question_text, hint)
    _start_timeout(bot, chat_id, topic_id, question_started_at)


async def _finish_quiz_and_notify(
    session: AsyncSession,
    bot: Bot,
    chat_id: int,
    topic_id: int,
    quiz_session: QuizSession,
) -> None:
    await end_quiz_session(session, quiz_session)
    key = (chat_id, topic_id)
    _cancel_answer_grace(chat_id, topic_id)
    results = _session_results.pop(key, {})
    winners = winners_from_results(results)

    bonus_line = ""
    if winners:
        winner_names: list[str] = []
        for user_id, name, _points in winners:
            await award_winner_bonus_coins(session, user_id, chat_id, display_name=name)
            winner_names.append(f"@{name}")
        bonus_line = (
            f"\n\nПобедитель(и) с +{QUIZ_WINNER_COINS_BONUS} монетами для игры в 21: "
            f"{', '.join(winner_names)}"
        )

    top_rows = await get_quiz_leaderboard(session, chat_id)
    leaderboard = ["Топ-5 за всё время:"]
    if top_rows:
        for idx, row in enumerate(top_rows, start=1):
            name = row.display_name or str(row.user_id)
            leaderboard.append(f"{idx}. @{name} — {row.total_points}")
    else:
        leaderboard.append("Пока пусто.")

    await session.commit()
    await bot.send_message(
        chat_id,
        f"Викторина завершена!\n\n{build_session_stats(results)}{bonus_line}\n\n" + "\n".join(leaderboard),
        message_thread_id=topic_id,
    )


@router.message(Command("umnij_start"))
async def start_quiz_admin(message: Message, bot: Bot) -> None:
    if settings.topic_games is None:
        return
    if message.chat.id != settings.forum_chat_id or message.message_thread_id != settings.topic_games:
        await message.reply("Команда доступна только в топике игры.")
        return
    if not await is_admin_message(bot, settings.forum_chat_id, message):
        await message.reply("Команда доступна только администраторам.")
        return

    started, reason = await _start_quiz(bot, settings.forum_chat_id, settings.topic_games, actor="admin")
    if not started:
        await message.reply(f"Ручной запуск не выполнен: {reason}")
        return

    await message.reply("Ручной запуск викторины выполнен.")


@router.message(Command("bal"))
async def add_quiz_point_admin(message: Message, bot: Bot) -> None:
    if not await is_admin_message(bot, settings.forum_chat_id, message):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Нужен реплай на сообщение пользователя, которому начисляем балл.")
        return

    target_user = message.reply_to_message.from_user
    display_name = target_user.username or target_user.full_name or str(target_user.id)

    async for session in get_session():
        stat = await award_point(session, target_user.id, settings.forum_chat_id, display_name=display_name)
        coin_stat = await award_correct_answer_coins(session, target_user.id, settings.forum_chat_id, display_name=display_name)
        await session.commit()
        break

    key = (settings.forum_chat_id, settings.topic_games or 0)
    if key not in _session_results:
        _session_results[key] = {}
    points = _session_results[key].get(target_user.id, (display_name, 0))[1] + 1
    _session_results[key][target_user.id] = (display_name, points)

    await message.reply(
        f"Начислен +1 балл и +{QUIZ_CORRECT_ANSWER_COINS} монет @{display_name}.\n"
        f"Очки: {stat.total_points}, Баланс: {coin_stat.coins} монет"
    )


@router.message(Command("topumnij"))
async def show_quiz_leaderboard(message: Message) -> None:
    async for session in get_session():
        top_players = await get_quiz_leaderboard(session, settings.forum_chat_id)
        break

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
    F.message_thread_id.is_not(None),
    (F.text | F.caption),
)
async def check_quiz_answer(message: Message, bot: Bot) -> None:
    message_text = message.text or message.caption
    if not message_text or message_text.startswith("/") or message.from_user is None:
        return
    if settings.topic_games is None:
        return

    chat_id = settings.forum_chat_id
    topic_id = settings.topic_games
    key = (chat_id, topic_id)

    async with _get_session_lock(chat_id, topic_id):
        async for session in get_session():
            quiz_session = await get_active_session(session, chat_id, topic_id)
            if not quiz_session:
                return

            question = await get_current_question(session, quiz_session)
            if not question:
                return

            ai_client = get_ai_client()
            decision = await ai_client.evaluate_quiz_answer(
                question.question,
                question.answer,
                message_text,
                chat_id=chat_id,
            )
            if not (decision.is_correct or decision.is_close):
                return

            display_name = _display_name(message) or str(message.from_user.id)
            if key not in _pending_answers:
                _pending_answers[key] = {}
            _pending_answers[key][message.from_user.id] = (display_name, decision.is_close and not decision.is_correct)

            if key in _answer_grace_tasks:
                await message.reply(
                    f"Ответ @{display_name} принят. Проверяю одновременно пришедшие ответы..."
                )
                return

            _cancel_timeout(chat_id, topic_id)
            _answer_grace_tasks[key] = asyncio.create_task(
                _finalize_answers_after_grace(
                    bot=bot,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    question_id=question.id,
                    question_started_at=quiz_session.question_started_at,
                )
            )
            await message.reply(
                f"Ответ @{display_name} принят. Даю 1 секунду на возможные одновременные ответы."
            )
            return


async def _finalize_answers_after_grace(
    bot: Bot,
    chat_id: int,
    topic_id: int,
    question_id: int,
    question_started_at: datetime | None,
) -> None:
    key = (chat_id, topic_id)
    try:
        await asyncio.sleep(1)
        async with _get_session_lock(chat_id, topic_id):
            accepted = _pending_answers.get(key, {})
            if not accepted:
                return

            async for session in get_session():
                quiz_session = await get_active_session(session, chat_id, topic_id)
                if not quiz_session or quiz_session.current_question_id != question_id:
                    return
                if quiz_session.question_started_at != question_started_at:
                    return

                question = await get_current_question(session, quiz_session)
                if not question:
                    return

                lines = [f"Правильный ответ: {question.answer}", "Баллы за вопрос:"]
                if key not in _session_results:
                    _session_results[key] = {}

                for user_id, (name, is_close) in accepted.items():
                    stat = await award_point(session, user_id, chat_id, display_name=name)
                    coin_stat = await award_correct_answer_coins(session, user_id, chat_id, display_name=name)
                    prev_points = _session_results[key].get(user_id, (name, 0))[1]
                    _session_results[key][user_id] = (name, prev_points + 1)
                    suffix = " (близкий ответ, засчитано ИИ)" if is_close else ""
                    lines.append(
                        f"• @{name} +1, +{QUIZ_CORRECT_ANSWER_COINS} монет "
                        f"(баланс: {coin_stat.coins}){suffix}"
                    )

                await bot.send_message(chat_id, "\n".join(lines), message_thread_id=topic_id)

                if await is_quiz_finished(quiz_session):
                    await _finish_quiz_and_notify(session, bot, chat_id, topic_id, quiz_session)
                    return

                quiz_session.current_question_id = None
                quiz_session.question_started_at = None
                await session.commit()
                break

        await _send_next_question_after_break(bot, chat_id, topic_id)
    finally:
        _answer_grace_tasks.pop(key, None)
        _pending_answers.pop(key, None)
