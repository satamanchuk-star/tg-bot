"""Клиент 2ГИС Routing API: время в пути с учётом пробок.

Используется в утреннем/вечернем отчёте «Попутчики», чтобы показать соседям
реальные минуты в пути от ЖК до метро Аннино и до МКАД (и обратно вечером).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10
# Запас для сравнения "с пробками vs свободно". 2ГИС не всегда возвращает
# отдельное поле "без пробок", поэтому используем total_duration как факт,
# а если есть free_duration / duration_no_traffic — кладём в free.


@dataclass(frozen=True)
class RouteInfo:
    label: str  # "ЖК → м.Аннино"
    duration_min: int  # с пробками
    duration_free_min: int | None  # без пробок (если API вернул)
    distance_km: float


def _point(lat: float, lon: float) -> dict:
    return {"type": "stop", "lat": lat, "lon": lon}


async def _fetch_route(label: str, src: tuple[float, float], dst: tuple[float, float]) -> RouteInfo | None:
    """Запрашивает маршрут src→dst у 2ГИС Routing API."""
    if not settings.dgis_api_key:
        logger.info("2ГИС: DGIS_API_KEY не задан, пропускаем запрос (%s).", label)
        return None

    payload = {
        "points": [_point(*src), _point(*dst)],
        "transport": "driving",
        "route_mode": "fastest",
        "traffic_mode": "jam",
        "output": "summary",
    }
    params = {"key": settings.dgis_api_key}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT)) as client:
            resp = await client.post(
                settings.dgis_routing_url,
                params=params,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("2ГИС: HTTP-ошибка для %s: %s", label, exc)
        return None
    except ValueError as exc:
        logger.warning("2ГИС: не смог распарсить JSON для %s: %s", label, exc)
        return None
    except Exception:
        logger.exception("2ГИС: неожиданная ошибка для %s", label)
        return None

    try:
        result = data.get("result") or []
        if not result:
            logger.warning("2ГИС: пустой result для %s (status=%s)", label, data.get("status"))
            return None
        first = result[0]
        duration_sec = int(first.get("total_duration") or 0)
        distance_m = int(first.get("total_distance") or 0)
        # Поле для свободного движения у 2ГИС называется по-разному
        # в разных версиях ответа. Берём что найдём.
        free_sec_raw = (
            first.get("total_duration_no_traffic")
            or first.get("free_duration")
            or first.get("ideal_duration")
        )
        free_sec = int(free_sec_raw) if free_sec_raw else None
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning("2ГИС: некорректная структура ответа для %s: %s", label, exc)
        return None

    if duration_sec <= 0:
        logger.warning("2ГИС: нулевая длительность для %s", label)
        return None

    return RouteInfo(
        label=label,
        duration_min=max(1, round(duration_sec / 60)),
        duration_free_min=max(1, round(free_sec / 60)) if free_sec else None,
        distance_km=round(distance_m / 1000, 1),
    )


def _home() -> tuple[float, float]:
    return settings.dgis_point_home_lat, settings.dgis_point_home_lon


def _annino() -> tuple[float, float]:
    return settings.dgis_point_annino_lat, settings.dgis_point_annino_lon


def _mkad() -> tuple[float, float]:
    return settings.dgis_point_mkad_lat, settings.dgis_point_mkad_lon


async def fetch_morning_routes() -> list[RouteInfo]:
    """Утренние направления: ИЗ ЖК до метро Аннино и до МКАД."""
    routes: list[RouteInfo] = []
    for label, src, dst in (
        ("ЖК → м.Аннино", _home(), _annino()),
        ("ЖК → МКАД", _home(), _mkad()),
    ):
        info = await _fetch_route(label, src, dst)
        if info is not None:
            routes.append(info)
    return routes


async def fetch_evening_routes() -> list[RouteInfo]:
    """Вечерние направления: до ЖК от метро Аннино и от МКАД."""
    routes: list[RouteInfo] = []
    for label, src, dst in (
        ("м.Аннино → ЖК", _annino(), _home()),
        ("МКАД → ЖК", _mkad(), _home()),
    ):
        info = await _fetch_route(label, src, dst)
        if info is not None:
            routes.append(info)
    return routes


def format_routes(routes: list[RouteInfo]) -> str:
    """Текстовая сводка для промпта LLM и для fallback-шаблона."""
    if not routes:
        return ""
    lines: list[str] = []
    for r in routes:
        if r.duration_free_min and r.duration_free_min < r.duration_min:
            tail = f" (без пробок ~{r.duration_free_min})"
        else:
            tail = ""
        lines.append(f"• {r.label}: {r.duration_min} мин{tail}, {r.distance_km} км")
    return "\n".join(lines)
