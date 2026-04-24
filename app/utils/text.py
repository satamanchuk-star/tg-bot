"""Почему: общий набор утилит для текстовой модерации."""

from __future__ import annotations

import re


LINK_PATTERN = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\w{3,}")

# Телефон: +7 (495) 401-60-06 / 8 495 401 60 06 / 8-800-100-20-30.
_PHONE_PATTERN = re.compile(
    r"(?:\+7|8)[\s\-\u00a0]*\(?\d{3}\)?[\s\-\u00a0]*\d{3}[\s\-\u00a0]*\d{2}[\s\-\u00a0]*\d{2}"
)
# Короткие номера (3-4 цифры): 112, 103, 8-800 уже ловится выше.
_SHORT_PHONE_PATTERN = re.compile(r"(?<![\w\d])(?:112|101|102|103|104|112)(?![\w\d])")

# URL: http(s)://..., www.foo.ru, а также голые домены вида site.ru/путь.
_URL_HTTP_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_URL_WWW_PATTERN = re.compile(r"(?<![\w@])www\.[\w\-.]+\.[a-zа-я]{2,}(?:/\S*)?", re.IGNORECASE)
_URL_BARE_PATTERN = re.compile(
    r"(?<![\w@/])(?:[a-z0-9][\w\-]*\.)+(?:ru|com|org|net|su|рф|info|pro|online|store|io)(?:/\S*)?",
    re.IGNORECASE,
)


def extract_phones(text: str) -> list[str]:
    """Возвращает уникальные телефонные номера из текста в исходном виде."""

    if not text:
        return []
    seen: set[str] = set()
    found: list[str] = []
    for match in _PHONE_PATTERN.finditer(text):
        phone = match.group(0).strip()
        digits = re.sub(r"\D", "", phone)
        if digits in seen:
            continue
        seen.add(digits)
        found.append(phone)
    return found


def phone_to_tel_uri(phone: str) -> str:
    """Нормализует номер до tel:+7XXXXXXXXXX."""

    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return f"tel:+{digits}"


def extract_urls(text: str) -> list[str]:
    """Возвращает уникальные URL из текста (http, www, голые домены)."""

    if not text:
        return []
    candidates: list[str] = []
    for pat in (_URL_HTTP_PATTERN, _URL_WWW_PATTERN, _URL_BARE_PATTERN):
        for match in pat.finditer(text):
            candidates.append(match.group(0))

    seen: set[str] = set()
    found: list[str] = []
    for raw in candidates:
        cleaned = raw.rstrip(").,;:!?»\"'")
        if not cleaned:
            continue
        key = cleaned.lower().rstrip("/")
        if key in seen:
            continue
        # Отсекаем поглощённые подстроки (голый домен внутри http://домен/).
        if any(key in other_key for other_key in seen):
            continue
        seen.add(key)
        found.append(cleaned)
    return found


def url_to_href(url: str) -> str:
    """Добавляет https:// если его не хватает, чтобы Telegram открывал кнопку."""

    if not url:
        return ""
    if url.lower().startswith(("http://", "https://")):
        return url
    if url.lower().startswith("www."):
        return "https://" + url
    return "https://" + url


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
