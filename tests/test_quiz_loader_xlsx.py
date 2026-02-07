"""Почему: проверяем чтение вопросов из XLSX с основного формата таблицы."""

from __future__ import annotations

from app.services.quiz_loader import QUIZ_XLSX_PATH, _read_xlsx_questions


def test_read_xlsx_questions_from_project_file() -> None:
    questions = _read_xlsx_questions(QUIZ_XLSX_PATH)
    assert questions
    first_q, first_a = questions[0]
    assert first_q.strip()
    assert first_a.strip()
