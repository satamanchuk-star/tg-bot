"""Экспорт в JSON."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..models import InfraObject
from ..constants import CATEGORY_SUBCATEGORY

logger = logging.getLogger(__name__)


def export_json(objects: list[InfraObject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for obj in objects:
        d = obj.model_dump()
        d["distance_km"] = round(d["distance_km"], 3)
        data.append(d)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("JSON: %d объектов -> %s", len(objects), path)


def export_category_dict(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(CATEGORY_SUBCATEGORY, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Category dictionary -> %s", path)
