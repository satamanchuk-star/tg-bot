"""Почему: факты в базе стареют, а бот этого не замечает.

График УК «ВЕК» поменялся, а бот полгода отдавал старый — пока владелец
случайно не прислал фото объявления. На октябрь 2026 у 47 из 57 записей базы
знаний последняя проверка была в марте, и в 32 из них — телефоны и часы.

Здесь — приписка к ответу ассистента: если в ответе есть телефон или часы
работы, которые мы давно не перепроверяли, бот честно говорит, когда они
проверены, и просит жителя нажать «⚠️ Устарело», если что-то изменилось.
Так база начинает обновляться силами жителей, без ручного аудита.

Свежесть считается ПО ФАКТУ, а не по записи: один и тот же телефон УК встречается
в десятке записей, и если хоть одна из них проверена недавно (или телефон
подтверждён карточкой места), факт свежий. Иначе подтверждённый номер
получал бы ложную приписку «проверено в марте» из-за старой соседней записи.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime

from sqlalchemy import select

from app.utils.text import extract_phones

logger = logging.getLogger(__name__)

# Через сколько дней факт считаем требующим перепроверки.
STALE_AFTER_DAYS = 180
# Карточки мест читаем из БД не на каждый ответ, а раз в 10 минут.
_PLACES_TTL_SECONDS = 600

_MONTHS_PREP = (
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
)

# Диапазон часов «09:00–18:00», «9.00 - 21.00». Одиночное время («13:00»)
# фактом не считаем: оно встречается слишком часто и ничего не идентифицирует.
_HOURS_RANGE = re.compile(
    r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*[–—-]\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b"
)

# Дата «неизвестно когда проверено» — заведомо старше любого порога.
_UNKNOWN = date.min

_places_cache: tuple[float, dict[str, date]] | None = None


def _facts(text: str) -> set[str]:
    """Проверяемые факты в тексте: телефоны (последние 10 цифр) и диапазоны часов."""
    found: set[str] = set()
    for phone in extract_phones(text or ""):
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 10:   # 112, 900, 3210 — общероссийские, не устаревают
            found.add("tel:" + digits[-10:])
    for m in _HOURS_RANGE.finditer(text or ""):
        found.add(f"hrs:{int(m[1])}:{m[2]}-{int(m[3])}:{m[4]}")
    return found


def _to_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return _UNKNOWN


def _merge(registry: dict[str, date], facts: set[str], checked: date) -> None:
    """Факт свежий, если ХОТЯ БЫ один источник проверен недавно — берём максимум."""
    for fact in facts:
        if checked > registry.get(fact, _UNKNOWN):
            registry[fact] = checked
        else:
            registry.setdefault(fact, checked)


def _kb_registry() -> dict[str, date]:
    from app.services.resident_kb import load_resident_kb

    registry: dict[str, date] = {}
    for entry in load_resident_kb():
        checked = _to_date(getattr(entry, "verified_at", None) or getattr(entry, "updated_at", None))
        _merge(registry, _facts(entry.answer), checked)
    return registry


async def _places_registry() -> dict[str, date]:
    global _places_cache
    now = time.monotonic()
    if _places_cache is not None and now - _places_cache[0] < _PLACES_TTL_SECONDS:
        return _places_cache[1]

    from app.db import get_session
    from app.models import Place

    registry: dict[str, date] = {}
    async for session in get_session():
        rows = (await session.execute(
            select(Place.phone, Place.work_time, Place.verified_at)
            .where(Place.is_active.is_(True))
        )).all()
        for phone, work_time, verified_at in rows:
            facts = _facts(f"{phone or ''} {work_time or ''}")
            _merge(registry, facts, _to_date(verified_at) if verified_at else _UNKNOWN)
        break
    _places_cache = (now, registry)
    return registry


def reset_cache() -> None:
    """Сброс кэша карточек мест (после правки данных и в тестах)."""
    global _places_cache
    _places_cache = None


async def freshness_note(reply: str, *, today: date | None = None) -> str | None:
    """Приписка о давности данных или None, если в ответе нет старых фактов.

    Смотрим только на факты, которые есть в НАШИХ данных (база знаний и
    карточки мест): номер, который модель взяла из веб-поиска, мы не
    проверяли и датировать не можем.
    """
    facts = _facts(reply)
    if not facts:
        return None
    registry = _kb_registry()
    try:
        places = await _places_registry()
    except Exception:  # noqa: BLE001 — без карточек мест работаем по базе знаний
        logger.debug("FRESHNESS: карточки мест недоступны.", exc_info=True)
        places = {}
    for fact, checked in places.items():
        _merge(registry, {fact}, checked)

    known = [registry[f] for f in facts if f in registry]
    if not known:
        return None
    oldest = min(known)
    today = today or date.today()
    if oldest != _UNKNOWN and (today - oldest).days <= STALE_AFTER_DAYS:
        return None
    if oldest == _UNKNOWN:
        when = "давно не перепроверялись"
    else:
        when = f"проверены в {_MONTHS_PREP[oldest.month - 1]} {oldest.year}"
    return f"ℹ️ Эти сведения {when}. Если что-то изменилось — нажмите «⚠️ Устарело», поправим."


async def with_freshness_note(reply: str) -> str:
    """Ответ с припиской о давности данных (если она нужна). Никогда не бросает:
    приписка — полезная мелочь, ради неё нельзя потерять сам ответ."""
    try:
        note = await freshness_note(reply)
    except Exception:  # noqa: BLE001
        logger.warning("FRESHNESS: не удалось оценить давность данных.", exc_info=True)
        return reply
    return f"{reply}\n\n{note}" if note else reply
