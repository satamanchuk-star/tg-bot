"""Почему: выделяем доменную логику викторины в единый сервис для переиспользования и тестов."""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizQuestion, QuizSession, QuizUsedQuestion, QuizUserStat, UserStat

QUIZ_QUESTIONS_COUNT = 10
QUIZ_QUESTION_TIMEOUT_SEC = 60
QUIZ_BREAK_BETWEEN_QUESTIONS_SEC = 60
QUIZ_WINNER_COINS_BONUS = 50


def get_used_question_ids(quiz_session: QuizSession) -> list[int]:
    if not quiz_session.used_question_ids:
        return []
    return json.loads(quiz_session.used_question_ids)


def _save_used_question_ids(quiz_session: QuizSession, ids: list[int]) -> None:
    quiz_session.used_question_ids = json.dumps(ids)


def _normalize_question_text(text: str) -> str:
    return " ".join(text.lower().split())


async def get_available_questions_count(session: AsyncSession) -> int:
    used_result = await session.execute(select(QuizUsedQuestion.question_normalized))
    used_questions = {row[0] for row in used_result.fetchall()}

    all_questions_result = await session.execute(select(QuizQuestion.question))
    available = [
        question_text
        for question_text in all_questions_result.scalars().all()
        if _normalize_question_text(question_text) not in used_questions
    ]
    return len(available)


async def can_start_quiz(
    session: AsyncSession,
    chat_id: int,
    topic_id: int,
) -> tuple[bool, str]:
    active = await get_active_session(session, chat_id, topic_id)
    if active:
        return False, "Викторина уже запущена в этом топике."

    available_count = await get_available_questions_count(session)
    if available_count < QUIZ_QUESTIONS_COUNT:
        return (
            False,
            "Недостаточно новых вопросов в базе: "
            f"нужно минимум {QUIZ_QUESTIONS_COUNT}, сейчас {available_count}.",
        )
    return True, ""


async def start_quiz_session(session: AsyncSession, chat_id: int, topic_id: int) -> QuizSession:
    quiz_session = QuizSession(chat_id=chat_id, topic_id=topic_id, is_active=True, question_number=0)
    session.add(quiz_session)
    await session.flush()
    return quiz_session


async def get_active_session(session: AsyncSession, chat_id: int, topic_id: int) -> QuizSession | None:
    result = await session.execute(
        select(QuizSession).where(
            QuizSession.chat_id == chat_id,
            QuizSession.topic_id == topic_id,
            QuizSession.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_random_question(session: AsyncSession, quiz_session: QuizSession | None = None) -> QuizQuestion | None:
    query = select(QuizQuestion)
    if quiz_session:
        used_ids = get_used_question_ids(quiz_session)
        if used_ids:
            query = query.where(QuizQuestion.id.notin_(used_ids))

    result = await session.execute(query)
    questions = list(result.scalars().all())
    if not questions:
        return None

    used_result = await session.execute(select(QuizUsedQuestion.question_normalized))
    used_questions = {row[0] for row in used_result.fetchall()}
    available = [q for q in questions if _normalize_question_text(q.question) not in used_questions]
    if not available:
        return None
    return random.choice(available)


async def set_current_question(session: AsyncSession, quiz_session: QuizSession, question: QuizQuestion) -> None:
    quiz_session.current_question_id = question.id
    quiz_session.question_number += 1
    quiz_session.question_started_at = datetime.now(timezone.utc)

    used_ids = get_used_question_ids(quiz_session)
    if question.id not in used_ids:
        used_ids.append(question.id)
        _save_used_question_ids(quiz_session, used_ids)

    normalized = _normalize_question_text(question.question)
    used_row = await session.get(QuizUsedQuestion, normalized)
    if used_row is None:
        session.add(QuizUsedQuestion(question_normalized=normalized))


async def get_current_question(session: AsyncSession, quiz_session: QuizSession) -> QuizQuestion | None:
    if quiz_session.current_question_id is None:
        return None
    return await session.get(QuizQuestion, quiz_session.current_question_id)


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]+", " ", text.lower())
    return " ".join(cleaned.split())


def _normalize_words(text: str) -> list[str]:
    normalized = _normalize_text(text)
    return normalized.split() if normalized else []


def _levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    prev = list(range(len(right) + 1))
    for i, lch in enumerate(left, start=1):
        curr = [i]
        for j, rch in enumerate(right, start=1):
            cost = 0 if lch == rch else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _match_words(correct_words: list[str], answer_words: list[str], needed_matches: int, typo_budget: int) -> bool:
    if not correct_words or not answer_words:
        return False

    pairs: list[tuple[int, int, int]] = []
    for c_idx, c_word in enumerate(correct_words):
        for a_idx, a_word in enumerate(answer_words):
            distance = _levenshtein_distance(c_word, a_word)
            if distance <= typo_budget:
                pairs.append((distance, c_idx, a_idx))

    pairs.sort(key=lambda item: item[0])
    used_c: set[int] = set()
    used_a: set[int] = set()
    total_typos = 0
    matches = 0

    for distance, c_idx, a_idx in pairs:
        if c_idx in used_c or a_idx in used_a:
            continue
        if total_typos + distance > typo_budget:
            continue
        used_c.add(c_idx)
        used_a.add(a_idx)
        total_typos += distance
        matches += 1
        if matches >= needed_matches:
            return True
    return False


def check_answer(question: QuizQuestion, answer: str) -> bool:
    """Проверяет ответ по правилам викторины с допуском опечаток и частичных совпадений."""
    correct_words = _normalize_words(question.answer)
    answer_words = _normalize_words(answer)
    if not correct_words or not answer_words:
        return False

    if len(correct_words) == 1:
        if len(answer_words) != 1:
            return False
        return _match_words(correct_words, answer_words, needed_matches=1, typo_budget=1)

    if len(correct_words) == 2:
        return _match_words(correct_words, answer_words, needed_matches=1, typo_budget=2)

    return _match_words(correct_words, answer_words, needed_matches=2, typo_budget=2)


async def award_point(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    display_name: str | None = None,
) -> QuizUserStat:
    stat = await session.get(QuizUserStat, (user_id, chat_id))
    if stat:
        stat.total_points += 1
        if display_name:
            stat.display_name = display_name
    else:
        stat = QuizUserStat(user_id=user_id, chat_id=chat_id, total_points=1, display_name=display_name)
        session.add(stat)
    return stat


async def end_quiz_session(session: AsyncSession, quiz_session: QuizSession) -> None:
    quiz_session.is_active = False
    quiz_session.current_question_id = None

    used_ids = get_used_question_ids(quiz_session)
    if used_ids:
        await session.execute(delete(QuizQuestion).where(QuizQuestion.id.in_(used_ids)))


async def get_quiz_leaderboard(session: AsyncSession, chat_id: int, limit: int = 5) -> list[QuizUserStat]:
    result = await session.execute(
        select(QuizUserStat)
        .where(QuizUserStat.chat_id == chat_id)
        .order_by(QuizUserStat.total_points.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def is_quiz_finished(quiz_session: QuizSession) -> bool:
    return quiz_session.question_number >= QUIZ_QUESTIONS_COUNT


async def award_winner_bonus_coins(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    display_name: str | None = None,
) -> UserStat:
    stats = await session.get(UserStat, {"user_id": user_id, "chat_id": chat_id})
    if stats is None:
        stats = UserStat(user_id=user_id, chat_id=chat_id, coins=100, display_name=display_name)
        session.add(stats)
    if display_name:
        stats.display_name = display_name
    stats.coins += QUIZ_WINNER_COINS_BONUS
    await session.flush()
    return stats


async def get_questions_left(session: AsyncSession) -> int:
    return await get_available_questions_count(session)


def build_answer_hint(answer: str) -> str:
    words_count = len(_normalize_words(answer))
    if words_count <= 1:
        return "Ответ: 1 слово."
    return "В ответе много слов."


def build_session_stats(results: dict[int, tuple[str, int]]) -> str:
    if not results:
        return "В этой сессии никто не ответил правильно."
    sorted_rows = sorted(results.values(), key=lambda item: item[1], reverse=True)
    lines = ["Статистика сессии:"]
    for name, points in sorted_rows:
        lines.append(f"• @{name}: {points}")
    return "\n".join(lines)


def winners_from_results(results: dict[int, tuple[str, int]]) -> list[tuple[int, str, int]]:
    if not results:
        return []
    top_points = max(points for _, points in results.values())
    return [
        (user_id, name, points)
        for user_id, (name, points) in results.items()
        if points == top_points
    ]
