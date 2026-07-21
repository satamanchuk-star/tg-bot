"""Почему: логика викторины отделена от aiogram — матч ответов и скоринг
тестируются без Telegram. Персистентность (QuizSession/QuizRound) рядом.

Главная забота — засчитывание ответов: многословные фразы с опечатками, но
числа/даты строго. Правило «первый верный забирает вопрос» делает начисление
атомарным (см. handlers/quiz.py), поэтому здесь только чистая проверка и БД.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizQuestion, QuizRound, QuizSession
from app.utils.morphology import lemmatize
from app.utils.time import ensure_aware

logger = logging.getLogger(__name__)

# --- Параметры тура (текст правил обязан им соответствовать) ---

QUESTIONS_PER_ROUND = 15
SECONDS_PER_QUESTION = 45
BREAK_SECONDS = 4
COINS_PER_CORRECT = 15
WINNER_BONUS = 100
STALE_SESSION_MINUTES = 10  # watchdog: сессия без прогресса дольше — закрыть

_STATE_VERSION = 1

# Стоп-слова, которые не должны решать засчёт (частые в ответах-фразах).
_STOP_WORDS = frozenset({
    "это", "был", "была", "было", "были", "в", "на", "и", "или", "а", "но",
    "то", "же", "бы", "он", "она", "они", "у", "с", "к", "по", "за", "из",
    "the", "a", "an", "of", "is",
})

# Разделители вариантов ответа в сид-данных: «Пётр Первый / Пётр I».
_ALT_SPLIT = re.compile(r"\s*[/;]\s*|\s+или\s+", re.IGNORECASE)


# --- Нормализация и матч ответов ---


def _normalize(text: str) -> str:
    """lower, ё→е, пунктуацию — в пробелы, схлопнуть пробелы."""
    lowered = text.lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    return " ".join(cleaned.split())


def _tokens(text: str) -> list[str]:
    return [t for t in _normalize(text).split() if t]


def _is_number(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


# Число словом ↔ цифрой: «сколько планет? — 8» засчитывает и «восемь», и «8».
# Без этого числовой ответ принимался только в одной форме (белое пятно).
_NUM_WORDS = {
    "ноль": "0", "один": "1", "одна": "1", "одно": "1", "два": "2", "две": "2",
    "три": "3", "четыре": "4", "пять": "5", "шесть": "6", "семь": "7",
    "восемь": "8", "девять": "9", "десять": "10", "одиннадцать": "11",
    "двенадцать": "12", "тринадцать": "13", "четырнадцать": "14",
    "пятнадцать": "15", "шестнадцать": "16", "семнадцать": "17",
    "восемнадцать": "18", "девятнадцать": "19", "двадцать": "20",
    "тридцать": "30", "сорок": "40", "пятьдесят": "50", "шестьдесят": "60",
    "семьдесят": "70", "восемьдесят": "80", "девяносто": "90", "сто": "100",
}


def _canon_number(token: str) -> str | None:
    """Каноничная цифровая форма токена, если он число или число-слово; иначе None."""
    if token.isdigit():
        return token
    return _NUM_WORDS.get(token)


def _bounded_levenshtein(a: str, b: str, max_dist: int = 1) -> int:
    """Расстояние Дамерау-Левенштейна с ранним выходом (max_dist+1, если больше).

    Перестановка соседних букв («сатурцаия» → «сатурация») считается ОДНОЙ
    правкой — это типичнейшая опечатка при быстрой печати в чате.
    """
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            # Транспозиция: ab ↔ ba за одну правку.
            if (
                prev2 is not None and i > 1 and j > 1
                and ca == b[j - 2] and a[i - 2] == cb
            ):
                val = min(val, prev2[j - 2] + 1)
            cur.append(val)
            best = min(best, val)
        if best > max_dist:
            return max_dist + 1
        prev2 = prev
        prev = cur
    return prev[-1]


def _token_matches(correct: str, given_tokens: list[str]) -> bool:
    """Найдётся ли в ответе токен, совпадающий с эталонным.

    Числа/даты — строго побуквенно (фикс бага «1939 принимал 1938»).
    Слова — по лемме или с опечаткой (Левенштейн ≤1 для длинных ≥5).
    """
    correct_num = _canon_number(correct)
    if correct_num is not None:
        # Числовой/числословный эталон: сверяем каноничные числа строго
        # (но «8» == «восемь»). «1939» никогда не примет «1938».
        return any(_canon_number(g) == correct_num for g in given_tokens)
    lemma_c = lemmatize(correct)
    for g in given_tokens:
        if g == correct or lemmatize(g) == lemma_c:
            return True
        # Опечатки прощаем только длинным словам (иначе «кот»≈«код»).
        if len(correct) >= 5 and not _is_number(g) and _bounded_levenshtein(correct, g, 1) <= 1:
            return True
    return False


def _variant_matched(variant: str, given_tokens: list[str]) -> bool:
    """Вариант эталона засчитан, если ВСЕ его значимые токены есть в ответе.

    Лишние слова в ответе игнорируются («это Москва» → «Москва» ок).
    """
    correct_tokens = [t for t in _tokens(variant) if t not in _STOP_WORDS]
    if not correct_tokens:
        correct_tokens = _tokens(variant)  # ответ целиком из стоп-слов — берём как есть
    if not correct_tokens:
        return False
    return all(_token_matches(c, given_tokens) for c in correct_tokens)


def check_answer(correct: str, given: str) -> bool:
    """Умеренный матч: опечатки прощаем, числа/даты — точно, лишние слова ок.

    Эталон может содержать альтернативы через «/», «;», «или» — достаточно
    совпасть с любой.
    """
    given_tokens = [t for t in _tokens(given) if t]
    if not given_tokens:
        return False
    variants = [v for v in _ALT_SPLIT.split(correct) if v.strip()]
    return any(_variant_matched(v, given_tokens) for v in variants)


def answer_length_hint(answer: str) -> str:
    """Подсказка о форме ответа без палева содержания."""
    first_variant = _ALT_SPLIT.split(answer)[0]
    words = _tokens(first_variant)
    if len(words) <= 1:
        return "одно слово" if not _is_number(first_variant) else "число"
    return f"{len(words)} слова"


# --- Состояние сессии (в QuizSession.state_json) ---


@dataclass
class QuizState:
    phase: str  # "asking" | "break" | "finished"
    question_ids: list[int]
    index: int = 0  # индекс текущего вопроса в question_ids
    current_answer: str = ""
    current_comment: str = ""  # пояснение к ответу (показывается при развязке)
    question_text: str = ""
    question_started_at: str = ""  # ISO с tz
    winner_user_id: int | None = None  # угадавший текущий вопрос (для first-wins)
    board_message_id: int | None = None
    scores: dict = field(default_factory=dict)  # {str(user_id): {"name": str, "correct": int}}
    updated_at: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "version": _STATE_VERSION,
            "phase": self.phase,
            "question_ids": self.question_ids,
            "index": self.index,
            "current_answer": self.current_answer,
            "current_comment": self.current_comment,
            "question_text": self.question_text,
            "question_started_at": self.question_started_at,
            "winner_user_id": self.winner_user_id,
            "board_message_id": self.board_message_id,
            "scores": self.scores,
            "updated_at": self.updated_at,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "QuizState | None":
        try:
            data = json.loads(payload)
            if data.get("version") != _STATE_VERSION:
                return None
            return cls(
                phase=str(data["phase"]),
                question_ids=[int(x) for x in data["question_ids"]],
                index=int(data.get("index", 0)),
                current_answer=str(data.get("current_answer", "")),
                current_comment=str(data.get("current_comment", "")),
                question_text=str(data.get("question_text", "")),
                question_started_at=str(data.get("question_started_at", "")),
                winner_user_id=data.get("winner_user_id"),
                board_message_id=data.get("board_message_id"),
                scores=dict(data.get("scores", {})),
                updated_at=str(data.get("updated_at", "")),
            )
        except (ValueError, KeyError, TypeError):
            return None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def is_stale(self, now: datetime) -> bool:
        if not self.updated_at:
            return True
        try:
            ts = ensure_aware(datetime.fromisoformat(self.updated_at))
        except ValueError:
            return True
        return now - ts > timedelta(minutes=STALE_SESSION_MINUTES)


def winners_from_scores(scores: dict) -> tuple[list[tuple[int, str, int]], int]:
    """Победители тура — все с максимумом правильных (>0). Возврат (список, max)."""
    best = 0
    for entry in scores.values():
        best = max(best, int(entry.get("correct", 0)))
    if best == 0:
        return [], 0
    winners = [
        (int(uid), entry.get("name") or str(uid), int(entry["correct"]))
        for uid, entry in scores.items()
        if int(entry.get("correct", 0)) == best
    ]
    return winners, best


# --- Персистентность QuizSession ---


async def load_session(session: AsyncSession, chat_id: int) -> QuizState | None:
    row = await session.get(QuizSession, chat_id)
    if row is None:
        return None
    state = QuizState.from_json(row.state_json)
    if state is None:
        await session.delete(row)
        await session.flush()
    return state


async def save_session(
    session: AsyncSession, chat_id: int, topic_id: int | None, state: QuizState
) -> None:
    state.touch()
    await session.merge(
        QuizSession(chat_id=chat_id, topic_id=topic_id, state_json=state.to_json())
    )
    await session.flush()


async def delete_session(session: AsyncSession, chat_id: int) -> None:
    await session.execute(delete(QuizSession).where(QuizSession.chat_id == chat_id))


async def get_active_chat_ids(session: AsyncSession) -> list[tuple[int, int | None]]:
    rows = (await session.execute(select(QuizSession))).scalars().all()
    return [(r.chat_id, r.topic_id) for r in rows]


# --- Выбор вопросов ---


async def count_fresh_questions(session: AsyncSession) -> int:
    """Сколько ещё не заданных вопросов осталось в пуле."""
    return int(await session.scalar(
        select(func.count()).select_from(QuizQuestion).where(QuizQuestion.used_at.is_(None))
    ) or 0)


async def pick_questions(session: AsyncSession, count: int) -> list[QuizQuestion]:
    """Берёт count НЕиспользованных вопросов. Повторов не бывает: когда свежие
    кончились — возвращает сколько есть, и викторина закрывается (решение
    владельца; recycle убран намеренно)."""
    fresh = (await session.execute(
        select(QuizQuestion).where(QuizQuestion.used_at.is_(None))
    )).scalars().all()
    chosen = random.sample(fresh, min(count, len(fresh)))
    now = datetime.now(timezone.utc)
    for q in chosen:
        q.used_at = now
    await session.flush()
    return chosen


async def get_question(session: AsyncSession, question_id: int) -> QuizQuestion | None:
    return await session.get(QuizQuestion, question_id)


# --- История и лидерборд ---


async def record_round(
    session: AsyncSession,
    *,
    chat_id: int,
    scores: dict,
    winner_ids: set[int],
    winner_bonus: int,
) -> None:
    """Пишет итоги тура на каждого участника (аудит, all-time топ)."""
    for uid, entry in scores.items():
        uid_int = int(uid)
        correct = int(entry.get("correct", 0))
        is_winner = uid_int in winner_ids
        coins = correct * COINS_PER_CORRECT + (winner_bonus if is_winner else 0)
        session.add(QuizRound(
            user_id=uid_int,
            chat_id=chat_id,
            correct_answers=correct,
            is_winner=is_winner,
            coins_awarded=coins,
            display_name=entry.get("name"),
        ))
    await session.flush()


async def get_alltime_leaderboard(
    session: AsyncSession, chat_id: int, limit: int = 5
) -> list[tuple[str, int, int]]:
    """Топ по сумме правильных ответов за всё время: (имя, правильных, побед)."""
    rows = (await session.execute(
        select(
            QuizRound.user_id,
            func.max(QuizRound.display_name),
            func.coalesce(func.sum(QuizRound.correct_answers), 0),
            func.coalesce(func.sum(func.cast(QuizRound.is_winner, Integer)), 0),
        )
        .where(QuizRound.chat_id == chat_id)
        .group_by(QuizRound.user_id)
        .order_by(desc(func.sum(QuizRound.correct_answers)))
        .limit(limit)
    )).all()
    return [(name or str(uid), int(correct), int(wins)) for uid, name, correct, wins in rows]
