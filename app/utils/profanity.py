"""Почему: выносим загрузку списка запрещенных слов в отдельный модуль."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict


class ProfanityRuntime(TypedDict):
    exact: set[str]
    prefixes: set[str]
    exceptions: set[str]


PROFANITY_PATH = Path(__file__).resolve().parent.parent / "data" / "profanity.txt"
PROFANITY_EXCEPTIONS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "profanity_exceptions.txt"
)


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


def load_profanity_exceptions() -> set[str]:
    """Загружает список исключений для мат-проверки."""

    if not PROFANITY_EXCEPTIONS_PATH.exists():
        return set()

    words: set[str] = set()
    for line in PROFANITY_EXCEPTIONS_PATH.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip().lower()
        if cleaned and not cleaned.startswith("#"):
            words.add(cleaned)
    return words


def split_profanity_words(words: set[str]) -> tuple[set[str], set[str]]:
    """Разделяет точные слова и префиксы (заканчивающиеся на *)."""

    exact: set[str] = set()
    prefixes: set[str] = set()
    for word in words:
        if word.endswith("*") and len(word) > 1:
            prefixes.add(word[:-1])
        else:
            exact.add(word)
    return exact, prefixes


_PROFANITY_RUNTIME: ProfanityRuntime = {"exact": set(), "prefixes": set(), "exceptions": set()}


def build_profanity_runtime(words: set[str], exceptions: set[str]) -> ProfanityRuntime:
    """Собирает runtime-словарь для быстрых проверок в памяти."""

    exact, prefixes = split_profanity_words(words)
    return {"exact": exact, "prefixes": prefixes, "exceptions": exceptions}


def reload_profanity_runtime() -> ProfanityRuntime:
    """Перезагружает runtime-словарь с диска и возвращает его."""

    global _PROFANITY_RUNTIME
    words = load_profanity()
    exceptions = load_profanity_exceptions()
    _PROFANITY_RUNTIME = build_profanity_runtime(words, exceptions)
    return get_profanity_runtime()


def get_profanity_runtime() -> ProfanityRuntime:
    """Возвращает копию runtime-словаря (чтобы не мутировали снаружи)."""

    return {
        "exact": set(_PROFANITY_RUNTIME["exact"]),
        "prefixes": set(_PROFANITY_RUNTIME["prefixes"]),
        "exceptions": set(_PROFANITY_RUNTIME["exceptions"]),
    }
