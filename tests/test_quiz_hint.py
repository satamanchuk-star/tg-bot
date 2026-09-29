"""Подсказка по ходу вопроса и время по длине текста.

Жалоба: вопросы в стиле «Что? Где? Когда?» слишком сложные для соседского
чата — вечер за вечером «Никто не успел». Лекарство: на середине вопроса бот
открывает первую букву и длину ответа, а длинным вопросам даёт больше времени.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, QuizQuestion


# --- Маска ответа ---

@pytest.mark.parametrize(("answer", "mask"), [
    ("Чумовые", "Ч _ _ _ _ _ _"),
    ("Рынок и кладбище", "Р _ _ _ _   _   _ _ _ _ _ _ _ _"),
    ("«На посошок»", "Н _   _ _ _ _ _ _ _"),        # кавычки не считаются буквами
    ("Стол-книжка", "С _ _ _ - _ _ _ _ _ _"),        # дефис виден
    ("Ёлка", "Ё _ _ _"),
    ("1939", "1 _ _ _"),
    ("Пётр Первый (зачёт: Пётр I)", "П _ _ _   _ _ _ _ _ _"),  # скобки выброшены
    ("Жюль Верн / Верн", "Ж _ _ _   _ _ _ _"),        # маска по первому варианту
])
def test_hint_mask(answer: str, mask: str) -> None:
    from app.services.quiz import hint_mask

    assert hint_mask(answer) == mask


@pytest.mark.parametrize("answer", ["Да", "I", "64"])
def test_short_answers_are_not_revealed(answer: str) -> None:
    """У «Да» подсказка «Д _» — уже готовый ответ, поэтому только длина."""
    from app.services.quiz import hint_mask

    mask = hint_mask(answer)
    assert set(mask.replace(" ", "")) == {"_"}


def test_hint_never_contains_the_answer_itself() -> None:
    """Подсказка открывает ровно одну букву — ответ целиком не утекает."""
    from app.services.quiz import hint_mask

    for answer in ("Шарлатан", "Безопасная бритва", "Мне всё фиолетово"):
        visible = [ch for ch in hint_mask(answer) if ch not in " _-"]
        assert len(visible) == 1


# --- Время по длине вопроса ---

def test_short_question_gets_base_time() -> None:
    from app.services.quiz import SECONDS_PER_QUESTION, question_seconds

    assert question_seconds("Столица Франции?") == SECONDS_PER_QUESTION
    assert question_seconds("слово " * 30) == SECONDS_PER_QUESTION


def test_long_question_gets_more_time_capped() -> None:
    from app.services.quiz import (
        EXTRA_SECONDS_MAX,
        SECONDS_PER_QUESTION,
        question_seconds,
    )

    medium = question_seconds("слово " * 55)
    huge = question_seconds("слово " * 300)
    assert SECONDS_PER_QUESTION < medium < huge
    assert huge == SECONDS_PER_QUESTION + EXTRA_SECONDS_MAX  # потолок
    assert (medium - SECONDS_PER_QUESTION) % 5 == 0          # ровные значения


def test_hint_opens_at_25th_second_of_base_question() -> None:
    from app.services.quiz import hint_at_seconds

    assert hint_at_seconds(45) == 25


def test_real_bank_timing_is_sane() -> None:
    """На настоящей базе: каждый вопрос получает от 45 до 75 секунд, а
    подсказка всегда открывается раньше предупреждения «10 секунд»."""
    import json
    from pathlib import Path

    from app.services.quiz import hint_at_seconds, question_seconds

    bank = json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "quiz_questions.json")
        .read_text(encoding="utf-8")
    )
    for item in bank:
        total = question_seconds(item["question"])
        assert 45 <= total <= 75
        assert hint_at_seconds(total) < total - 10


# --- Текст вопроса и вехи ---

def _state(answer: str = "Чумовые", text: str = "Как назвали тур?"):
    from app.services.quiz import QuizState

    return QuizState(
        phase="asking", question_ids=[1, 2, 3], index=0,
        current_answer=answer, question_text=text,
    )


def test_question_text_shows_hint_only_when_asked() -> None:
    from app.handlers.quiz import _question_text

    state = _state()
    plain = _question_text(state)
    hinted = _question_text(state, hint=True)
    warned = _question_text(state, hint=True, warn=True)
    assert "Подсказка" not in plain
    assert "🔤 Подсказка: Ч _ _ _ _ _ _" in hinted
    assert "Подсказка" in warned and "Осталось 10 секунд" in warned


def test_question_text_shows_own_time_for_long_question() -> None:
    from app.handlers.quiz import _question_text
    from app.services.quiz import question_seconds

    long_text = "слово " * 60
    text = _question_text(_state(text=long_text))
    assert f"{question_seconds(long_text)} сек" in text


def test_stages_order_and_tiny_questions() -> None:
    from app.handlers.quiz import _stages

    assert _stages(45) == [(20.0, "hint"), (10.0, "warn")]
    # Вопросы по 1-3 секунды (как в тестах драйвера) — без вех вовсе.
    assert _stages(2) == []


def _run_wait(monkeypatch, remaining: float) -> list[str]:
    """Прогоняет ожидание с уже «взятым» вопросом и возвращает правки на вехах."""
    from app.handlers import quiz as h

    shown: list[str] = []

    async def _record(bot, chat_id, kind):
        shown.append(kind)

    monkeypatch.setattr(h, "_reveal_stage", _record)

    async def run():
        h._answer_events.pop(555, None)
        h._event_for(555).set()   # ответ уже пришёл — ждать нечего
        await h._wait_with_stages(None, 555, _state(), remaining)

    asyncio.run(run())
    h._answer_events.pop(555, None)
    return shown


def test_fresh_question_shows_nothing_before_answer(monkeypatch) -> None:
    """Вопрос взяли сразу — ни подсказки, ни предупреждения."""
    assert _run_wait(monkeypatch, remaining=45) == []


def test_resume_after_restart_catches_up_hint(monkeypatch) -> None:
    """Бот перезапустился на 30-й секунде: подсказку открываем сразу."""
    assert _run_wait(monkeypatch, remaining=15) == ["hint"]


def test_resume_near_end_shows_warn_once(monkeypatch) -> None:
    """На последних секундах догоняем одной правкой — предупреждение уже
    содержит подсказку, двух правок подряд не нужно."""
    assert _run_wait(monkeypatch, remaining=5) == ["warn"]


# --- Живой прогон драйвера ---

@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/quiz.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _get_session():
        async with factory() as session:
            yield session

    asyncio.run(_prepare())
    monkeypatch.setattr("app.handlers.quiz.get_session", _get_session)
    yield factory
    asyncio.run(engine.dispose())


def _make_bot() -> AsyncMock:
    bot = AsyncMock()
    counter = {"n": 0}

    async def _send(*args, **kwargs):
        counter["n"] += 1
        return SimpleNamespace(message_id=7000 + counter["n"], chat=SimpleNamespace(id=100))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


def test_driver_reveals_hint_mid_question(db, monkeypatch) -> None:
    """Никто не отвечает — на середине вопроса сообщение правится подсказкой."""
    from app.handlers import quiz as h

    monkeypatch.setattr(h.settings, "forum_chat_id", 100)
    monkeypatch.setattr(h.settings, "topic_games", 42)
    monkeypatch.setattr(h.q, "QUESTIONS_PER_ROUND", 3)
    monkeypatch.setattr(h.q, "SECONDS_PER_QUESTION", 4)   # подсказка на 2-й секунде
    monkeypatch.setattr(h.q, "BREAK_SECONDS", 0)
    h._chat_locks.clear()
    h._answer_events.clear()
    h._running.clear()

    async def run():
        async with db() as session:
            for i, (question, answer) in enumerate([
                ("Царь зверей?", "Лев"),
                ("Столица Франции?", "Париж"),
                ("Спутник Земли?", "Луна"),
            ]):
                session.add(QuizQuestion(id=i + 1, question=question, answer=answer))
            await session.commit()
        bot = _make_bot()
        assert await h._launch_quiz(bot, 100) is None
        await asyncio.sleep(3.0)
        for task in list(h._running.values()):
            task.cancel()
        return bot

    bot = asyncio.run(run())
    edits = [c.args[0] for c in bot.edit_message_text.call_args_list if c.args]
    hinted = [e for e in edits if "🔤 Подсказка:" in e]
    assert hinted, f"подсказка так и не появилась, правки: {edits}"
    # Подсказка относится к первому вопросу и не выдаёт ответ целиком.
    assert any(mask in hinted[0] for mask in ("Л _ _", "П _ _ _ _", "Л _ _ _"))
