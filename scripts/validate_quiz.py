"""Почему: вопросы попадают в игру только пройдя валидацию — иначе бот будет
задавать кривые вопросы или не засчитывать верные ответы.

Два уровня проверки:
1. Валидация ВОПРОСА — форма (самодостаточность, короткий ответ, без медиа).
2. Валидация ОТВЕТА — реальным алгоритмом матча: эталонный ответ обязан
   засчитываться сам себе (иначе игрок не сможет ответить правильно никогда).

Запуск вручную: python -m scripts.validate_quiz  → печатает проблемы.
Тот же валидатор гоняется в CI над data/quiz_questions.json (тест).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.quiz import _ALT_SPLIT, _tokens, check_answer

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "quiz_questions.json"

# Вопрос не самодостаточен, если ссылается на медиа/контекст, которого у игрока нет.
_MEDIA_MARKERS = (
    "картинк", "рисунк", "фото", "изображен", "на экране", "см.", "смотри",
    "по ссылке", "в видео", "на слайде", "выше", "ниже приведен",
)
# Маркеры неоднозначного ответа — такому вопросу не место в авто-викторине.
_VAGUE_MARKERS = ("возможно", "зависит", "разные", "и т.д", "и т. д", "может быть")

_MAX_ANSWER_WORDS = 4  # эталонный ответ должен быть коротким


def _variants(answer: str) -> list[str]:
    return [v.strip() for v in _ALT_SPLIT.split(answer) if v.strip()]


def validate_one(item: dict) -> list[str]:
    """Список проблем одной пары (пусто = валидна)."""
    issues: list[str] = []
    question = str(item.get("question", "")).strip()
    answer = str(item.get("answer", "")).strip()

    if not question:
        issues.append("пустой вопрос")
    if not answer:
        issues.append("пустой ответ")
    if issues:
        return issues

    q_lower = question.lower()
    for marker in _MEDIA_MARKERS:
        if marker in q_lower:
            issues.append(f"вопрос ссылается на медиа/контекст: «{marker}»")
            break

    a_lower = answer.lower()
    for marker in _VAGUE_MARKERS:
        if marker in a_lower:
            issues.append(f"неоднозначный ответ: «{marker}»")
            break

    variants = _variants(answer)
    if not variants:
        issues.append("ответ пуст после разбора вариантов")
        return issues

    from app.services.quiz import _STOP_WORDS

    for v in variants:
        words = _tokens(v)
        if len(words) > _MAX_ANSWER_WORDS:
            issues.append(f"слишком длинный вариант ответа ({len(words)} слов): «{v}»")
        if not words:
            issues.append(f"вариант ответа без значимых токенов: «{v}»")
        elif all(w in _STOP_WORDS for w in words):
            # Эталон из одних стоп-слов («это») засчитал бы любую фразу с ним.
            issues.append(f"вариант ответа целиком из стоп-слов: «{v}»")

    # Валидация ОТВЕТА матчем: каждый вариант эталона обязан засчитаться сам себе.
    for v in variants:
        if not check_answer(answer, v):
            issues.append(f"матч не засчитывает собственный ответ: «{v}» на «{answer}»")

    return issues


def validate_questions(items: list[dict]) -> list[str]:
    """Проверяет весь список. Возвращает человекочитаемые проблемы (пусто = ок)."""
    problems: list[str] = []
    seen_questions: dict[str, int] = {}

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"#{i}: не объект")
            continue
        # Дубликаты вопросов (по нормализованному тексту).
        norm_q = " ".join(_tokens(str(item.get("question", ""))))
        if norm_q and norm_q in seen_questions:
            problems.append(f"#{i}: дубль вопроса #{seen_questions[norm_q]}: «{item.get('question')}»")
        elif norm_q:
            seen_questions[norm_q] = i
        for issue in validate_one(item):
            problems.append(f"#{i} «{str(item.get('question'))[:50]}»: {issue}")
    return problems


def load_and_validate() -> tuple[int, list[str]]:
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    items = [d for d in data if isinstance(d, dict) and "question" in d]
    return len(items), validate_questions(items)


if __name__ == "__main__":
    count, problems = load_and_validate()
    print(f"Вопросов в базе: {count}")
    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {len(problems)}\n")
        for p in problems:
            print(" •", p)
        raise SystemExit(1)
    print("✅ Все вопросы валидны.")
