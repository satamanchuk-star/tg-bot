"""Слияние дублей InfraObject."""

from __future__ import annotations

from ..models import InfraObject

# Приоритет источников (меньше = лучше)
_SOURCE_PRIORITY = {
    "official": 1,
    "yandex_maps": 2,
    "google_maps": 3,
    "2gis": 4,
    "regional_portal": 5,
    "aggregator": 6,
    "static": 7,
}


def _source_rank(source: str) -> int:
    """Числовой приоритет источника (меньше = лучше)."""
    s = source.lower().split(";")[0].strip()
    for key, rank in _SOURCE_PRIORITY.items():
        if key in s:
            return rank
    return 99


def _pick_best(val_a: str | None, val_b: str | None, a_rank: int, b_rank: int) -> str | None:
    """Выбрать лучшее значение: непустое от более приоритетного источника."""
    if val_a and val_b:
        return val_a if a_rank <= b_rank else val_b
    return val_a or val_b


def _merge_sources(src_a: str, src_b: str) -> str:
    """Объединить строки источников без дублей."""
    parts_a = {s.strip() for s in src_a.split(";") if s.strip()}
    parts_b = {s.strip() for s in src_b.split(";") if s.strip()}
    return "; ".join(sorted(parts_a | parts_b))


def merge_objects(a: InfraObject, b: InfraObject) -> InfraObject:
    """Объединить два дубля в один объект."""
    rank_a = _source_rank(a.source)
    rank_b = _source_rank(b.source)
    primary, secondary = (a, b) if rank_a <= rank_b else (b, a)

    # Описание: объединить без дублей
    desc_parts = []
    for desc in (primary.description, secondary.description):
        if desc and desc not in desc_parts:
            desc_parts.append(desc)
    description = "; ".join(desc_parts) if desc_parts else None

    # is_active: осторожный подход
    if a.is_active != b.is_active:
        is_active = primary.is_active
        if description:
            description += "; Конфликт is_active между источниками"
        else:
            description = "Конфликт is_active между источниками"
    else:
        is_active = a.is_active

    return InfraObject(
        name=primary.name,
        category=primary.category,
        subcategory=primary.subcategory,
        address=primary.address or secondary.address,
        phone=_merge_field_multi(a.phone, b.phone),
        website=_pick_best(a.website, b.website, rank_a, rank_b),
        work_time=_pick_best(a.work_time, b.work_time, rank_a, rank_b),
        description=description,
        lat=primary.lat,
        lon=primary.lon,
        distance_km=primary.distance_km,
        source=_merge_sources(a.source, b.source),
        is_active=is_active,
    )


def _merge_field_multi(val_a: str | None, val_b: str | None) -> str | None:
    """Объединить мульти-значения (телефоны) через '; '."""
    parts = set()
    for val in (val_a, val_b):
        if val:
            for p in val.split(";"):
                p = p.strip()
                if p:
                    parts.add(p)
    return "; ".join(sorted(parts)) if parts else None
