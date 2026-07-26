"""Матч ответов викторины: реальные жалобы игроков + защита от ложных засчётов.

Жалоба владельца (июль 2026): бот не засчитывал очевидно верные ответы —
«Фиолетово» на «Мне всё фиолетово», «Юниоры» на «Джуниоры», «Бритва» на
«Безопасная бритва». Люди из-за этого бросали игру.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from app.services.quiz import check_answer, is_near_miss


# --- Реальные случаи из чата: обязаны засчитываться ---

@pytest.mark.parametrize(("correct", "given"), [
    # Q12: ответ-фраза, игрок пишет суть (местоимения — не часть ответа)
    ("Мне всё фиолетово", "Фиолетово"),
    ("Мне всё фиолетово", "фиолетово"),
    ("Мне всё фиолетово", "мне все фиолетово"),
    ("Мне всё фиолетово", "фиолетовый"),
    # Q9: другое написание заимствования
    ("Джуниоры", "Юниоры"),
    ("Джуниоры", "юниор"),
    ("Джуниоры", "джуниоры"),
    # Q13: ключевое слово вместо полного словосочетания
    ("Безопасная бритва", "Бритва"),
    ("Безопасная бритва", "бритву"),
    ("Безопасная бритва", "это бритва наверное"),
    # Тот же класс — фамилия вместо полного имени
    ("Лев Толстой", "Толстой"),
    ("Александр Пушкин", "пушкин"),
])
def test_real_player_answers_are_accepted(correct: str, given: str) -> None:
    assert check_answer(correct, given) is True


# --- Защита: послабления не должны засчитывать чужой ответ ---

@pytest.mark.parametrize(("correct", "given"), [
    # Различающий модификатор потерян — ответ неоднозначен
    ("Первая мировая война", "война"),
    ("Вторая мировая война", "первая мировая война"),
    ("Северный полюс", "полюс"),
    ("Красная площадь", "площадь"),
    ("Пётр Первый", "Пётр Третий"),
    # Слишком общая «голова» словосочетания
    ("Тихий океан", "океан"),
    ("Чёрное море", "море"),
    ("Стиральная машина", "машина"),
    # Числа — всегда строго
    ("1939", "1938"),
    ("Аполлон 11", "аполлон"),
    ("Аполлон 11", "аполлон 13"),
    # Просто другое слово
    ("Джуниоры", "сеньоры"),
    ("Мне всё фиолетово", "мне всё равно"),
    # Отрицание не засчитывается (регрессия прошлой жалобы)
    ("Париж", "это не Париж"),
])
def test_wrong_answers_are_rejected(correct: str, given: str) -> None:
    assert check_answer(correct, given) is False


def test_near_miss_marks_close_answers() -> None:
    """«Почти» — совпало значимое слово многословного эталона (реакция 👀)."""
    assert is_near_miss("Первая мировая война", "война") is True
    assert is_near_miss("Тихий океан", "океан") is True
    # Засчитанный ответ «почти» не бывает
    assert is_near_miss("Безопасная бритва", "бритва") is False
    # Мимо — это мимо
    assert is_near_miss("Джуниоры", "сеньоры") is False
    assert is_near_miss("Тихий океан", "велосипед") is False


# --- Эмпирика на настоящей базе вопросов ---

_BANK = Path(__file__).resolve().parent.parent / "data" / "quiz_questions.json"


def _bank_answers() -> list[str]:
    with open(_BANK, encoding="utf-8") as f:
        return [q["answer"] for q in json.load(f)]


def test_every_bank_answer_matches_itself() -> None:
    """Санити: эталон обязан засчитывать сам себя — иначе вопрос непроходим."""
    broken = [a for a in _bank_answers() if not check_answer(a, a)]
    assert broken == []


def test_cross_answer_false_accept_rate_is_low() -> None:
    """Ответ на ЧУЖОЙ вопрос почти никогда не должен проходить.

    Порог 1%: на момент фикса — 0.12% (5 из 4000 пар), причём большинство из
    них — корректное поведение («Фары» ← «Выключают фары»). Тест держит
    послабления матчера в узде: разъедутся — упадёт здесь.
    """
    answers = _bank_answers()
    rng = random.Random(42)
    pairs = [(rng.choice(answers), rng.choice(answers)) for _ in range(2000)]
    false_accepts = [(c, g) for c, g in pairs if c != g and check_answer(c, g)]
    rate = len(false_accepts) / len(pairs)
    assert rate < 0.01, f"ложных засчётов {rate:.2%}: {false_accepts[:10]}"


def test_key_word_credit_covers_most_multiword_answers() -> None:
    """Многословные ответы должны браться ключевым словом — ради этого фикс.

    Если доля упадёт, значит матчер снова стал придирчивым (игроки уходят).
    """
    from app.services.quiz import _content_tokens

    multi = [a for a in _bank_answers() if len(_content_tokens(a)) >= 2]
    accepted = [a for a in multi if check_answer(a, _content_tokens(a)[-1])]
    assert len(accepted) / len(multi) > 0.5
