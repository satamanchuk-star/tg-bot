"""Дедупликация инфраструктурных объектов."""

from __future__ import annotations

import logging

from rapidfuzz import fuzz

from ..config import DEDUP_DISTANCE_THRESHOLD_M, DEDUP_NAME_SIMILARITY_THRESHOLD
from ..models import InfraObject, ValidationIssue
from .geo import haversine_km
from .normalizers import make_dedup_key
from .merger import merge_objects

logger = logging.getLogger(__name__)


def deduplicate(
    objects: list[InfraObject],
) -> tuple[list[InfraObject], list[ValidationIssue]]:
    """Дедуплицировать список объектов.

    Возвращает (уникальные_объекты, issues_о_слияниях).
    """
    if not objects:
        return [], []

    issues: list[ValidationIssue] = []

    # Группируем по dedup-ключу
    groups: dict[str, list[InfraObject]] = {}
    for obj in objects:
        key = make_dedup_key(obj.name, obj.address)
        groups.setdefault(key, []).append(obj)

    # Объединяем группы
    merged: list[InfraObject] = []
    for key, group in groups.items():
        if len(group) > 1:
            issues.append(ValidationIssue(
                raw_name=group[0].name,
                raw_address=group[0].address,
                reason="duplicate_merged",
                details=f"Объединено {len(group)} записей по ключу",
            ))
        merged.append(_merge_group(group))

    # Второй проход: поиск дублей по расстоянию + похожести названия
    result = _proximity_dedup(merged, issues)

    logger.info("Дедупликация: %d -> %d объектов", len(objects), len(result))
    return result, issues


def _merge_group(group: list[InfraObject]) -> InfraObject:
    """Объединить группу дублей в один объект."""
    result = group[0]
    for other in group[1:]:
        result = merge_objects(result, other)
    return result


def _proximity_dedup(
    objects: list[InfraObject],
    issues: list[ValidationIssue],
) -> list[InfraObject]:
    """Дополнительная дедупликация по близости координат + похожести названий."""
    threshold_km = DEDUP_DISTANCE_THRESHOLD_M / 1000.0
    used = set()
    result: list[InfraObject] = []

    for i, obj_a in enumerate(objects):
        if i in used:
            continue
        current = obj_a
        for j in range(i + 1, len(objects)):
            if j in used:
                continue
            obj_b = objects[j]
            if obj_a.category != obj_b.category:
                continue
            dist = haversine_km(obj_a.lat, obj_a.lon, obj_b.lat, obj_b.lon)
            if dist > threshold_km:
                continue
            name_sim = fuzz.ratio(obj_a.name.lower(), obj_b.name.lower()) / 100.0
            if name_sim >= DEDUP_NAME_SIMILARITY_THRESHOLD:
                current = merge_objects(current, obj_b)
                used.add(j)
                issues.append(ValidationIssue(
                    raw_name=obj_b.name,
                    raw_address=obj_b.address,
                    reason="duplicate_merged",
                    details=f"Расстояние {dist*1000:.0f}м, сходство имён {name_sim:.0%}",
                ))
        result.append(current)

    return result
