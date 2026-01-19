"""Почему: выносим загрузку списка запрещенных слов в отдельный модуль."""

from __future__ import annotations

from pathlib import Path


PROFANITY_PATH = Path(__file__).resolve().parent.parent / "data" / "profanity.txt"


def load_profanity() -> set[str]:
    """Загружает список запрещенных слов из файла."""

    if not PROFANITY_PATH.exists():
        return set()

    words: set[str] = set()
    for line in PROFANITY_PATH.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip().lower()
        if cleaned and not cleaned.startswith("#"):
            words.add(cleaned)
    return words
