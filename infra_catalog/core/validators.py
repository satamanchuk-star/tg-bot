"""Валидация объектов."""

from __future__ import annotations

from ..models import InfraObject, ValidationIssue
from ..constants import validate_category_pair


def validate(obj: InfraObject, provider: str = "") -> list[ValidationIssue]:
    """Вернуть список проблем валидации (пустой = ок)."""
    issues: list[ValidationIssue] = []

    if not obj.name:
        issues.append(ValidationIssue(
            provider=provider, raw_name=obj.name, raw_address=obj.address,
            reason="missing_name", details="Отсутствует название объекта",
        ))

    if not obj.category or not obj.subcategory:
        issues.append(ValidationIssue(
            provider=provider, raw_name=obj.name, raw_address=obj.address,
            reason="invalid_category_mapping",
            details=f"category={obj.category!r}, subcategory={obj.subcategory!r}",
        ))
    elif not validate_category_pair(obj.category, obj.subcategory):
        issues.append(ValidationIssue(
            provider=provider, raw_name=obj.name, raw_address=obj.address,
            reason="invalid_category_mapping",
            details=f"Недопустимая пара: {obj.category}/{obj.subcategory}",
        ))

    if not obj.address and (obj.lat == 0.0 and obj.lon == 0.0):
        issues.append(ValidationIssue(
            provider=provider, raw_name=obj.name, raw_address=obj.address,
            reason="no_coordinates",
            details="Нет ни адреса, ни координат",
        ))

    return issues
