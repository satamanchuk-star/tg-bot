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
_CONCURRENCY = 5  # одновременных проверок URL — вежливо к источникам, но не последовательно
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


async def _check_url(client: httpx.AsyncClient, url: str) -> str | None:
    """Проверка одиночного URL (для ссылок из ответов KB) — та же логика."""
    try:
        resp = await client.get(url, headers={"User-Agent": _UA})
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if resp.status_code in (404, 410):
        return f"ссылка недоступна (HTTP {resp.status_code}): {url}"
    if resp.status_code >= 400:
        return None
    lowered = resp.text[:200_000].lower()
    for marker in _CLOSED_MARKERS:
        if marker in lowered:
            return f"на странице маркер «{marker}»: {url}"
    return None


def _kb_urls() -> list[tuple[str, str]]:
    """(id записи KB, url) — ссылки из ответов базы знаний для сверки.

    Симметрия свежести: телефоны позвонить нельзя, а сайты (УК, порталы,
    школы) — можно; умершая ссылка в ответе KB — сигнал устаревшей записи.
    """
    try:
        from app.services.resident_kb import load_resident_kb
        pairs: list[tuple[str, str]] = []
        for e in load_resident_kb():
            m = _URL_RE.search(getattr(e, "answer", "") or "")
            if m:
                url = m.group(0).rstrip(".»)")
                if not url.startswith("https://docs.google.com"):  # формы живут отдельно
                    pairs.append((e.id, url))
        return pairs
    except Exception:
        logger.warning("PLACE_VERIFY: не удалось собрать ссылки KB.", exc_info=True)
        return []


async def verify_places(bot: Bot) -> None:
    """Еженедельный job: сверка активных мест (и ссылок KB) с первоисточниками."""
    try:
        async for session in get_session():
            places = (await session.execute(
                select(Place).where(Place.is_active.is_(True))
            )).scalars().all()
            break
        else:
            return

        suspicions: list[str] = []
        sem = asyncio.Semaphore(_CONCURRENCY)
        kb_links = _kb_urls()

        async def _guarded(place: Place) -> tuple[Place, str | None]:
            async with sem:
                return place, await _check_place(client, place)

        async def _guarded_kb(kb_id: str, url: str) -> tuple[str, str | None]:
            async with sem:
                return kb_id, await _check_url(client, url)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT), follow_redirects=True,
        ) as client:
            results = await asyncio.gather(*(_guarded(p) for p in places))
            kb_results = await asyncio.gather(*(_guarded_kb(i, u) for i, u in kb_links))

        for place, reason in results:
            if reason:
                suspicions.append(f"• {place.name} ({place.category}): {reason}")
        for kb_id, reason in kb_results:
            if reason:
                suspicions.append(f"• KB «{kb_id}»: {reason} — проверьте запись в resident_kb.json")

        if not suspicions:
            logger.info(
                "PLACE_VERIFY: %d мест и %d ссылок KB проверено, подозрений нет.",
                len(places), len(kb_links),
            )
            return

        text = (
            f"🔎 Еженедельная сверка инфраструктуры: {len(places)} мест, "
            f"{len(kb_links)} ссылок KB, "
            f"подозрений — {len(suspicions)}:\n\n" + "\n".join(suspicions[:20]) +
            "\n\nПроверьте вручную. Закрылось — is_active=false в "
            "data/places_seed.json (или в Google Sheet) + /kb_reload."
        )
        await bot.send_message(settings.admin_log_chat_id, text)
        logger.info("PLACE_VERIFY: отчёт с %d подозрениями отправлен.", len(suspicions))
    except Exception:
        logger.warning("PLACE_VERIFY: сверка не удалась.", exc_info=True)
