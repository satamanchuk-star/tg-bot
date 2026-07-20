"""CI-гейт качества базы вопросов: каждый вопрос проходит валидацию формы,
и его эталонный ответ засчитывается реальным алгоритмом матча.

Этот тест — часть конвейера «база → валидация вопросов → валидация ответов →
игра»: в игру попадают только вопросы, прошедшие проверку.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.quiz import QUESTIONS_PER_ROUND
from scripts.validate_quiz import SEED_FILE, validate_questions


def _load() -> list[dict]:
    data = json.loads(Path(SEED_FILE).read_text(encoding="utf-8"))
    return [d for d in data if isinstance(d, dict) and "question" in d]


def test_question_base_is_valid() -> None:
    items = _load()
    problems = validate_questions(items)
    assert not problems, "Невалидные вопросы:\n" + "\n".join(problems[:20])


def test_base_has_enough_for_a_round() -> None:
    """Вопросов должно хватать хотя бы на несколько туров без повторов."""
    items = _load()
    assert len(items) >= QUESTIONS_PER_ROUND * 3


def test_every_answer_matches_itself() -> None:
    """Валидация ОТВЕТОВ: эталон обязан засчитываться сам себе (иначе игрок
    физически не сможет ответить верно)."""
    from app.services.quiz import _ALT_SPLIT, check_answer

    items = _load()
    broken = []
    for item in items:
        answer = item["answer"]
        for variant in _ALT_SPLIT.split(answer):
            variant = variant.strip()
            if variant and not check_answer(answer, variant):
                broken.append(f"{item['question']} → {answer} (вариант «{variant}»)")
    assert not broken, "Ответы не матчатся сами себе:\n" + "\n".join(broken[:20])
