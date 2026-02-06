"""Почему: фиксируем, что ответ принимается только при совпадении числа слов."""

from __future__ import annotations

from app.models import QuizQuestion
from app.services.quiz import check_answer


def test_check_answer_accepts_single_word_with_small_typo() -> None:
    question = QuizQuestion(question="Столица Франции?", answer="Париж")

    assert check_answer(question, "Парих") is True


def test_check_answer_rejects_extra_words_for_single_word_answer() -> None:
    question = QuizQuestion(question="Столица Франции?", answer="Париж")

    assert check_answer(question, "Париж Франция") is False


def test_check_answer_rejects_missing_words_for_multi_word_answer() -> None:
    question = QuizQuestion(question="Кто написал Войну и мир?", answer="Лев Толстой")

    assert check_answer(question, "Толстой") is False
