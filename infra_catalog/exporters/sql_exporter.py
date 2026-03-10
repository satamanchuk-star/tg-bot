"""Генерация SQL seed-файла для импорта данных."""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import InfraObject

logger = logging.getLogger(__name__)


def _escape_sql(val: str | None) -> str:
    if val is None:
        return "NULL"
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"


def export_sql_seed(objects: list[InfraObject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "-- Auto-generated seed data for infra_objects",
        "-- Encoding: UTF-8",
        "",
        "BEGIN;",
        "",
    ]

    for obj in objects:
        cols = (
            "name, category, subcategory, address, phone, website, "
            "work_time, description, lat, lon, distance_km, source, is_active"
        )
        vals = ", ".join([
            _escape_sql(obj.name),
            _escape_sql(obj.category),
            _escape_sql(obj.subcategory),
            _escape_sql(obj.address),
            _escape_sql(obj.phone),
            _escape_sql(obj.website),
            _escape_sql(obj.work_time),
            _escape_sql(obj.description),
            str(obj.lat),
            str(obj.lon),
            str(round(obj.distance_km, 3)),
            _escape_sql(obj.source),
            str(obj.is_active).lower(),
        ])
        lines.append(f"INSERT INTO infra_objects ({cols}) VALUES ({vals});")

    lines.extend(["", "COMMIT;", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("SQL seed: %d объектов -> %s", len(objects), path)
