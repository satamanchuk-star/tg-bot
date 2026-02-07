"""Почему: фиксируем правила проверки ответов и подсказки формата ответа."""

from __future__ import annotations

from app.models import QuizQuestion
from app.services.quiz import build_answer_hint, check_answer


def test_check_answer_single_word_accepts_one_typo() -> None:
    question = QuizQuestion(question="Столица Франции?", answer="Париж")
    assert check_answer(question, "Парих") is True


def test_check_answer_single_word_rejects_extra_words() -> None:
    question = QuizQuestion(question="Столица Франции?", answer="Париж")
    assert check_answer(question, "Париж Франция") is False


def test_check_answer_two_words_accepts_one_matching_word() -> None:
    question = QuizQuestion(question="Кто написал Войну и мир?", answer="Лев Толстой")
    assert check_answer(question, "Толстой") is True


def test_check_answer_three_words_requires_two_matches() -> None:
    question = QuizQuestion(question="Автор романа Евгений Онегин?", answer="Александр Сергеевич Пушкин")
    assert check_answer(question, "Пушкин") is False
    assert check_answer(question, "Александр Пушкин") is True


def test_build_answer_hint() -> None:
    assert build_answer_hint("Париж") == "Ответ: 1 слово."
    assert build_answer_hint("Лев Толстой") == "В ответе много слов."
