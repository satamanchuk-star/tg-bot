"""Почему: общий набор утилит для текстовой модерации."""
from __future__ import annotations

import re


URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
TELEGRAM_LINK_PATTERN = re.compile(r"https?://t\.me/\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\w{3,}")


def contains_forbidden_link(text: str) -> bool:
    """Возвращает True, если найден внешний линк (кроме телеграм-ссылок)."""

    if not URL_PATTERN.search(text):
        return False
    if TELEGRAM_LINK_PATTERN.search(text):
        return False
    return True


def normalize_words(text: str) -> list[str]:
    """Разбивает текст на слова для простого поиска запретных слов."""

    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return [word for word in cleaned.split() if word]
