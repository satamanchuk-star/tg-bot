"""Экспорт в CSV."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from ..models import InfraObject, ValidationIssue

logger = logging.getLogger(__name__)

_FIELDS = [
    "name", "category", "subcategory", "address", "phone", "website",
    "work_time", "description", "lat", "lon", "distance_km", "source", "is_active",
]


def export_csv(objects: list[InfraObject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        for obj in objects:
            row = obj.model_dump()
            row["distance_km"] = round(row["distance_km"], 3)
            writer.writerow({k: row.get(k, "") for k in _FIELDS})
    logger.info("CSV: %d объектов -> %s", len(objects), path)


def export_issues_csv(issues: list[ValidationIssue], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["provider", "raw_name", "raw_address", "reason", "details"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for issue in issues:
            writer.writerow(issue.model_dump())
    logger.info("Issues CSV: %d записей -> %s", len(issues), path)
