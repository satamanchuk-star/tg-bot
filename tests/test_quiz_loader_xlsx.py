"""Почему: проверяем чтение вопросов из XLSX с основного формата таблицы."""

from __future__ import annotations

from app.services.quiz_loader import QUIZ_XLSX_PATH, _read_xlsx_questions
import pytest


def test_read_xlsx_questions_from_project_file() -> None:
    if not QUIZ_XLSX_PATH.exists():
        pytest.skip("Файл viktorinavopros_QA.xlsx отсутствует в окружении")
    questions = _read_xlsx_questions(QUIZ_XLSX_PATH)
    assert questions
    first_q, first_a = questions[0]
    assert first_q.strip()
    assert first_a.strip()
