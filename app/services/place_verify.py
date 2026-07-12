"""Почему: инфраструктура вокруг строящегося ЖК устаревает быстро — раз в неделю
бот сам сверяет места с первоисточниками и сообщает админам о подозрениях.

Проверка best-effort по URL из поля source (карточки Яндекс/2GIS, офсайты):
- страница недоступна (404/410) — сигнал «проверить вручную»;
- в тексте карточки маркер «закрыто навсегда» — сильный сигнал закрытия.
Ложные срабатывания возможны — поэтому только алерт админам, никаких
автоматических отключений записей.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.models import Place

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_URL_RE = re.compile(r"https?://[^\s;,)]+")
# Маркеры закрытия в карточках Яндекс.Карт / 2GIS / зумерских справочников
_CLOSED_MARKERS = (
    "закрыто навсегда",
    "закрыт навсегда",
    "больше не работает",
    "организация закрыта",
    "permanently closed",
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _first_url(source: str | None) -> str | None:
    if not source:
        return None
    m = _URL_RE.search(source)
    return m.group(0) if m else None


async def _check_place(client: httpx.AsyncClient, place: Place) -> str | None:
    """Возвращает текст подозрения или None, если всё выглядит живым."""
    url = _first_url(place.source)
    if url is None:
        return None
    try:
        resp = await client.get(url, headers={"User-Agent": _UA})
    except (httpx.HTTPError, httpx.TimeoutException):
        # Сеть/таймаут — не повод для алерта (переходящее)
        return None
    if resp.status_code in (404, 410):
        return f"источник недоступен (HTTP {resp.status_code}): {url}"
    if resp.status_code >= 400:
        return None  # 403/429 и т.п. — антибот, не сигнал
    lowered = resp.text[:200_000].lower()
    for marker in _CLOSED_MARKERS:
        if marker in lowered:
            return f"на странице источника маркер «{marker}»: {url}"
    return None


async def verify_places(bot: Bot) -> None:
    """Еженедельный job: сверка активных мест с первоисточниками, алерт в лог-чат."""
    try:
        async for session in get_session():
            places = (await session.execute(
                select(Place).where(Place.is_active.is_(True))
            )).scalars().all()
            break
        else:
            return

        suspicions: list[str] = []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT), follow_redirects=True,
        ) as client:
            for place in places:
                reason = await _check_place(client, place)
                if reason:
                    suspicions.append(f"• {place.name} ({place.category}): {reason}")
                await asyncio.sleep(1.0)  # вежливая пауза между запросами

        if not suspicions:
            logger.info("PLACE_VERIFY: %d мест проверено, подозрений нет.", len(places))
            return

        text = (
            f"🔎 Еженедельная сверка инфраструктуры: {len(places)} мест, "
            f"подозрений — {len(suspicions)}:\n\n" + "\n".join(suspicions[:20]) +
            "\n\nПроверьте вручную. Закрылось — is_active=false в "
            "data/places_seed.json (или в Google Sheet) + /kb_reload."
        )
        await bot.send_message(settings.admin_log_chat_id, text)
        logger.info("PLACE_VERIFY: отчёт с %d подозрениями отправлен.", len(suspicions))
    except Exception:
        logger.warning("PLACE_VERIFY: сверка не удалась.", exc_info=True)
