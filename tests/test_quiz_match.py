"""Тесты засчитывания ответов викторины — главная боль старой версии.

Требования владельца: умеренно (опечатки прощаем, лишние слова ок), но
числа/даты — строго. Каждый кейс тут — это то, что старая версия делала не так.
"""

from __future__ import annotations

from app.services.quiz import answer_length_hint, check_answer, winners_from_scores


def test_exact_single_word() -> None:
    assert check_answer("Москва", "Москва") is True
    assert check_answer("Москва", "москва") is True  # регистр
    assert check_answer("Москва", "МОСКВА") is True


def test_extra_words_allowed() -> None:
    """«это Москва» на «Москва» — засчитать (старая версия отклоняла)."""
    assert check_answer("Москва", "это Москва") is True
    assert check_answer("Снеговик", "это снеговик") is True
    assert check_answer("кислород", "думаю кислород наверное") is True


def test_typos_forgiven_for_long_words() -> None:
    assert check_answer("Москва", "масква") is True
    assert check_answer("кислород", "кисларод") is True
    assert check_answer("Достоевский", "достоевскй") is True


def test_short_word_typos_not_over_forgiven() -> None:
    """Короткие слова не прощаем по опечатке («кот»≠«код»)."""
    assert check_answer("кот", "код") is False
    assert check_answer("лев", "лес") is False


def test_numbers_must_be_exact() -> None:
    """Фикс главного бага: «1939» не должно принимать «1938»."""
    assert check_answer("1939", "1939") is True
    assert check_answer("1939", "1938") is False
    assert check_answer("8", "6") is False
    assert check_answer("206", "207") is False
    assert check_answer("300000", "300001") is False


def test_number_with_extra_words() -> None:
    assert check_answer("1961", "в 1961 году") is True
    assert check_answer("1961", "думаю 1962") is False


def test_number_word_equivalence() -> None:
    """Белое пятно: «8» должно принимать «восемь» и наоборот."""
    assert check_answer("8", "восемь") is True
    assert check_answer("восемь", "8") is True
    assert check_answer("8", "8") is True
    assert check_answer("восемь", "восемь") is True
    # Но соседнее число словом — по-прежнему мимо.
    assert check_answer("8", "семь") is False
    assert check_answer("восемь", "9") is False
    # Годы — только цифрами (в словах их всё равно не пишут).
    assert check_answer("1961", "1961") is True


def test_multiword_requires_all_significant() -> None:
    """Многословный ответ: нужны все значимые слова (умеренно, не 40%)."""
    assert check_answer("Красная площадь", "красная площадь") is True
    assert check_answer("Красная площадь", "площадь") is False  # не хватает «красная»
    assert check_answer("Млечный путь", "млечны путь") is True  # опечатка в длинном слове


def test_alternatives_via_slash() -> None:
    """Варианты ответа через «/» — засчитать любой."""
    assert check_answer("Пётр Первый / Пётр I / Пётр", "пётр") is True
    assert check_answer("Пётр Первый / Пётр I / Пётр", "петр первый") is True
    assert check_answer("Эверест / Джомолунгма", "джомолунгма") is True
    assert check_answer("восемь / 8", "8") is True
    assert check_answer("восемь / 8", "восемь") is True


def test_yo_normalization() -> None:
    assert check_answer("Пётр", "петр") is True
    assert check_answer("зелёный", "зеленый") is True


def test_empty_and_garbage() -> None:
    assert check_answer("Москва", "") is False
    assert check_answer("Москва", "   ") is False
    assert check_answer("Москва", "привет как дела") is False


def test_lemma_matching_cases() -> None:
    """Падежи/формы: лемматизация ловит «в Москве» → «Москва»."""
    assert check_answer("Москва", "в Москве") is True
    assert check_answer("кислород", "кислорода") is True


def test_answer_length_hint() -> None:
    assert answer_length_hint("Москва") == "одно слово"
    assert answer_length_hint("1961") == "число"
    assert "2" in answer_length_hint("Красная площадь")
    # По первому варианту альтернативы
    assert answer_length_hint("Пётр Первый / Пётр") == "2 слова"


def test_winners_from_scores() -> None:
    scores = {
        "1": {"name": "Аня", "correct": 3},
        "2": {"name": "Петя", "correct": 3},
        "3": {"name": "Ваня", "correct": 1},
    }
    winners, best = winners_from_scores(scores)
    assert best == 3
    assert {w[0] for w in winners} == {1, 2}  # оба лидера

    assert winners_from_scores({}) == ([], 0)
    # Никто не набрал очков → нет победителей
    assert winners_from_scores({"1": {"name": "X", "correct": 0}}) == ([], 0)
