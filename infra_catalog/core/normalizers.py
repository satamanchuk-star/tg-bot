"""Нормализация данных: телефоны, адреса, названия, URL."""

from __future__ import annotations

import re
import unicodedata

try:
    import phonenumbers

    def normalize_phone(raw: str) -> str:
        """Нормализовать телефонный номер (или несколько через ';')."""
        if not raw or not raw.strip():
            return ""
        parts = re.split(r"[;,]", raw)
        results = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # Попробовать распарсить
            try:
                parsed = phonenumbers.parse(part, "RU")
                if phonenumbers.is_valid_number(parsed):
                    results.append(
                        phonenumbers.format_number(
                            parsed, phonenumbers.PhoneNumberFormat.E164
                        )
                    )
                    continue
            except phonenumbers.NumberParseException:
                pass
            # Fallback — очистить
            cleaned = re.sub(r"[^\d+]", "", part)
            if cleaned:
                results.append(cleaned)
        return "; ".join(results)

except ImportError:

    def normalize_phone(raw: str) -> str:
        """Упрощённая нормализация без phonenumbers."""
        if not raw or not raw.strip():
            return ""
        parts = re.split(r"[;,]", raw)
        results = []
        for part in parts:
            cleaned = re.sub(r"[^\d+]", "", part.strip())
            if not cleaned:
                continue
            # 8XXXXXXXXXX -> +7XXXXXXXXXX
            if cleaned.startswith("8") and len(cleaned) == 11:
                cleaned = "+7" + cleaned[1:]
            elif cleaned.startswith("7") and len(cleaned) == 11:
                cleaned = "+" + cleaned
            results.append(cleaned)
        return "; ".join(results)


def normalize_text(text: str) -> str:
    """Базовая очистка строки."""
    if not text:
        return ""
    # Убрать невидимые Unicode-символы (кроме пробелов)
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith("C") or ch in ("\n", "\t", " ")
    )
    text = text.replace("\t", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_name(name: str) -> str:
    return normalize_text(name)


def normalize_address(address: str) -> str:
    return normalize_text(address)


def normalize_website(url: str) -> str:
    if not url or not url.strip():
        return ""
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def normalize_work_time(wt: str) -> str:
    return normalize_text(wt)


# --- ключ для дедупликации ---

_ADDRESS_REPLACEMENTS = [
    (r"\bулица\b", "ул"),
    (r"\bул\.\s*", "ул "),
    (r"\bдом\b", "д"),
    (r"\bд\.\s*", "д "),
    (r"\bкорпус\b", "к"),
    (r"\bкорп\.\s*", "к "),
    (r"\bстроение\b", "стр"),
    (r"\bстр\.\s*", "стр "),
    (r"\bгород\b", "г"),
    (r"\bг\.\s*", "г "),
    (r"\bпоселок\b", "пос"),
    (r"\bпос\.\s*", "пос "),
    (r"\bрайон\b", "р-н"),
    (r"\bобласть\b", "обл"),
    (r"\bобл\.\s*", "обл "),
]


def make_dedup_key(name: str, address: str) -> str:
    """Нормализованный ключ для дедупликации."""
    s = f"{name}|{address}".lower().strip()
    s = s.replace("ё", "е")
    s = re.sub(r"[«»\"'`]", "", s)
    # Нормализовать "№ 2" -> "№2" (убрать пробел после №)
    s = re.sub(r"№\s+", "№", s)
    for pattern, repl in _ADDRESS_REPLACEMENTS:
        s = re.sub(pattern, repl, s, flags=re.IGNORECASE)
    # Убрать "г " / "г." перед названием города (часто опускается)
    s = re.sub(r"\bг\s+", "", s)
    # Убрать скобочные суффиксы типа "(пункт выдачи)"
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
