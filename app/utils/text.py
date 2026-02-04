"""Почему: общий набор утилит для текстовой модерации."""

from __future__ import annotations

import re


LINK_PATTERN = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\w{3,}")


def contains_forbidden_link(text: str) -> bool:
    """Возвращает True, если найден любой линк."""

    return bool(LINK_PATTERN.search(text))


def normalize_words(text: str) -> list[str]:
    """Разбивает текст на слова для простого поиска запретных слов."""

    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return [word for word in cleaned.split() if word]


def contains_profanity(
    words: list[str],
    exact_words: set[str],
    prefixes: set[str],
    exceptions: set[str],
) -> bool:
    """Проверяет наличие матных слов с учетом исключений и префиксов."""

    for word in words:
        if word in exceptions:
            continue
        if word in exact_words:
            return True
        if any(word.startswith(prefix) for prefix in prefixes):
            return True
    return False
