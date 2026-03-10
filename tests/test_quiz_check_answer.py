"""Почему: фиксируем правила локальной проверки ответов викторины без ИИ."""

from __future__ import annotations

from app.models import QuizQuestion
from app.services.quiz import check_answer


def _q(answer: str) -> QuizQuestion:
    return QuizQuestion(id=1, question="Вопрос", answer=answer)


def test_single_word_requires_single_token() -> None:
    question = _q("Снеговик")
    assert check_answer(question, "снеговик") is True
    assert check_answer(question, "это снеговик") is False


def test_two_word_answer_requires_both_words() -> None:
    question = _q("Лев Толстой")
    assert check_answer(question, "Толстой") is False
    assert check_answer(question, "Лев Толстой") is True


def test_multi_word_answer_accepts_meaningful_subset() -> None:
    question = _q("александр сергеевич пушкин русский поэт")
    assert check_answer(question, "пушкин поэт") is True
    assert check_answer(question, "пушкин") is False


def test_multi_word_answer_allows_up_to_three_typos() -> None:
    question = _q("домофон подъезд шлагбаум")
    assert check_answer(question, "домофан подезд") is True
