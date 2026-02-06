"""Почему: проверяем, что текстовый загрузчик вопросов работает стабильно."""

from __future__ import annotations

from pathlib import Path

from app.services.quiz_loader import _read_text_questions


def test_read_text_questions_supports_multiple_separators(tmp_path: Path) -> None:
    file_path = tmp_path / "quiz_questions.txt"
    file_path.write_text(
        "# комментарий\n"
        "Вопрос 1?|Ответ 1\n"
        "Вопрос 2?;Ответ 2\n"
        "Вопрос 3?\tОтвет 3\n"
        "Некорректная строка без разделителя\n",
        encoding="utf-8",
    )

    questions = _read_text_questions(file_path)

    assert questions == [
        ("Вопрос 1?", "Ответ 1"),
        ("Вопрос 2?", "Ответ 2"),
        ("Вопрос 3?", "Ответ 3"),
    ]


def test_read_text_questions_normalizes_whitespace(tmp_path: Path) -> None:
    file_path = tmp_path / "quiz_questions.txt"
    file_path.write_text("  Вопрос    1?   |   Ответ    1  \n", encoding="utf-8")

    questions = _read_text_questions(file_path)

    assert questions == [("Вопрос 1?", "Ответ 1")]
