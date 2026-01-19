"""Почему: общий набор утилит для текстовой модерации."""

from __future__ import annotations

import re


URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
TELEGRAM_LINK_PATTERN = re.compile(r"^(https?://)?t\.me/\S+$", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\w{3,}")


def contains_forbidden_link(text: str) -> bool:
    """Возвращает True, если найден внешний линк (кроме телеграм-ссылок)."""

    urls = URL_PATTERN.findall(text)
    if not urls:
        return False
    return any(not TELEGRAM_LINK_PATTERN.match(url) for url in urls)


def normalize_words(text: str) -> list[str]:
    """Разбивает текст на слова для простого поиска запретных слов."""

    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return [word for word in cleaned.split() if word]
