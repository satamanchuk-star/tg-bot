"""Почему: фиксируем поведение ИИ-fallback для оценки ответов викторины и подсказки формата."""

from __future__ import annotations

from app.services.ai_module import local_quiz_answer_decision
from app.services.quiz import build_answer_hint


def test_local_quiz_answer_exact_match_is_correct() -> None:
    decision = local_quiz_answer_decision("Лев Толстой", "лев толстой")
    assert decision.is_correct is True


def test_local_quiz_answer_partial_match_is_close() -> None:
    decision = local_quiz_answer_decision("Александр Сергеевич Пушкин", "Пушкин")
    assert decision.is_correct is False
    assert decision.is_close is True


def test_local_quiz_answer_wrong_is_not_close() -> None:
    decision = local_quiz_answer_decision("Париж", "Берлин")
    assert decision.is_correct is False
    assert decision.is_close is False


def test_build_answer_hint() -> None:
    assert build_answer_hint("Париж") == "Ответ: 1 слово."
    assert build_answer_hint("Лев Толстой") == "В ответе много слов."
