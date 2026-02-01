"""Почему: логика викторины изолирована от хендлеров."""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizDailyLimit, QuizQuestion, QuizSession, QuizUserStat
from app.utils.time import is_game_time_allowed, now_tz

QUIZ_MAX_LAUNCHES_PER_DAY = 2
QUIZ_QUESTIONS_COUNT = 10
QUIZ_QUESTION_TIMEOUT_SEC = 60


def get_used_question_ids(quiz_session: QuizSession) -> list[int]:
    """Возвращает список ID использованных вопросов."""
    if not quiz_session.used_question_ids:
        return []
    return json.loads(quiz_session.used_question_ids)


def add_used_question_id(quiz_session: QuizSession, question_id: int) -> None:
    """Добавляет ID вопроса в список использованных."""
    used_ids = get_used_question_ids(quiz_session)
    if question_id not in used_ids:
        used_ids.append(question_id)
        quiz_session.used_question_ids = json.dumps(used_ids)


async def can_start_quiz(
    session: AsyncSession,
    chat_id: int,
    topic_id: int,
) -> tuple[bool, str]:
    """Проверяет возможность запуска викторины.

    Возвращает (можно_ли, причина_отказа).
    """
    # Проверка времени: 20:00-22:00 МСК
    if not is_game_time_allowed(20, 22):
        return False, "Викторина доступна с 20:00 до 22:00 по Москве."

    # Проверка активной сессии
    active = await session.execute(
        select(QuizSession).where(
            QuizSession.chat_id == chat_id,
            QuizSession.topic_id == topic_id,
            QuizSession.is_active == True,
        )
    )
    if active.scalar_one_or_none():
        return False, "Викторина уже запущена в этом топике."

    # Проверка дневного лимита
    date_key = now_tz().date().isoformat()
    limit_row = await session.get(
        QuizDailyLimit, (chat_id, topic_id, date_key)
    )
    if limit_row and limit_row.launches >= QUIZ_MAX_LAUNCHES_PER_DAY:
        return False, f"Достигнут лимит викторин на сегодня ({QUIZ_MAX_LAUNCHES_PER_DAY})."

    # Проверка наличия достаточного количества вопросов
    count_result = await session.execute(select(func.count(QuizQuestion.id)))
    questions_count = count_result.scalar()
    if questions_count < QUIZ_QUESTIONS_COUNT:
        return False, f"Недостаточно вопросов в базе (нужно минимум {QUIZ_QUESTIONS_COUNT}, сейчас {questions_count}). Пополните базу вопросов."

    return True, ""


async def start_quiz_session(
    session: AsyncSession,
    chat_id: int,
    topic_id: int,
) -> QuizSession:
    """Создаёт новую сессию викторины."""
    # Увеличиваем счётчик дневного лимита
    date_key = now_tz().date().isoformat()
    limit_row = await session.get(QuizDailyLimit, (chat_id, topic_id, date_key))
    if limit_row:
        limit_row.launches += 1
    else:
        session.add(QuizDailyLimit(
            chat_id=chat_id,
            topic_id=topic_id,
            date_key=date_key,
            launches=1,
        ))

    quiz_session = QuizSession(
        chat_id=chat_id,
        topic_id=topic_id,
        is_active=True,
        question_number=0,
    )
    session.add(quiz_session)
    await session.flush()
    return quiz_session


async def get_active_session(
    session: AsyncSession,
    chat_id: int,
    topic_id: int,
) -> QuizSession | None:
    """Возвращает активную сессию викторины."""
    result = await session.execute(
        select(QuizSession).where(
            QuizSession.chat_id == chat_id,
            QuizSession.topic_id == topic_id,
            QuizSession.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def get_random_question(
    session: AsyncSession,
    quiz_session: QuizSession | None = None,
) -> QuizQuestion | None:
    """Возвращает случайный вопрос из БД, исключая использованные в сессии."""
    query = select(QuizQuestion)
    if quiz_session:
        exclude_ids = get_used_question_ids(quiz_session)
        if exclude_ids:
            query = query.where(QuizQuestion.id.notin_(exclude_ids))

    result = await session.execute(query)
    questions = result.scalars().all()
    if not questions:
        return None
    return random.choice(questions)


async def set_current_question(
    session: AsyncSession,
    quiz_session: QuizSession,
    question: QuizQuestion,
) -> None:
    """Устанавливает текущий вопрос для сессии и помечает его как использованный."""
    quiz_session.current_question_id = question.id
    quiz_session.question_number += 1
    quiz_session.question_started_at = datetime.now(timezone.utc)
    add_used_question_id(quiz_session, question.id)


async def get_current_question(
    session: AsyncSession,
    quiz_session: QuizSession,
) -> QuizQuestion | None:
    """Возвращает текущий вопрос сессии."""
    if quiz_session.current_question_id is None:
        return None
    return await session.get(QuizQuestion, quiz_session.current_question_id)


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]+", " ", text.lower())
    return " ".join(cleaned.split())


def _normalize_words(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    return normalized.split()


def check_answer(question: QuizQuestion, answer: str) -> bool:
    """Проверяет ответ на вопрос без учёта регистра и пунктуации."""
    correct_words = _normalize_words(question.answer)
    answer_words = _normalize_words(answer)
    if not correct_words or not answer_words:
        return False
    if len(correct_words) == 1:
        return _normalize_text(question.answer) == _normalize_text(answer)
    return len(set(correct_words) & set(answer_words)) >= 2


def is_question_timed_out(quiz_session: QuizSession) -> bool:
    """Проверяет, истекло ли время на ответ."""
    if quiz_session.question_started_at is None:
        return False
    started = quiz_session.question_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return elapsed >= QUIZ_QUESTION_TIMEOUT_SEC


async def award_point(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    display_name: str | None = None,
) -> QuizUserStat:
    """Начисляет балл пользователю."""
    stat = await session.get(QuizUserStat, (user_id, chat_id))
    if stat:
        stat.total_points += 1
        if display_name:
            stat.display_name = display_name
    else:
        stat = QuizUserStat(
            user_id=user_id,
            chat_id=chat_id,
            total_points=1,
            display_name=display_name,
        )
        session.add(stat)
    return stat


async def end_quiz_session(
    session: AsyncSession,
    quiz_session: QuizSession,
) -> None:
    """Завершает сессию викторины."""
    quiz_session.is_active = False
    quiz_session.current_question_id = None


async def get_quiz_leaderboard(
    session: AsyncSession,
    chat_id: int,
    limit: int = 5,
) -> list[QuizUserStat]:
    """Возвращает топ игроков по очкам."""
    result = await session.execute(
        select(QuizUserStat)
        .where(QuizUserStat.chat_id == chat_id)
        .order_by(QuizUserStat.total_points.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def is_quiz_finished(quiz_session: QuizSession) -> bool:
    """Проверяет, завершена ли викторина (10 вопросов)."""
    return quiz_session.question_number >= QUIZ_QUESTIONS_COUNT
