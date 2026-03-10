"""Провайдер, загружающий данные из статических JSON/CSV файлов."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from ..models import RawObject
from .base import BaseProvider

logger = logging.getLogger(__name__)


class StaticLoaderProvider(BaseProvider):
    """Загружает RawObject из JSON или CSV файлов в указанной директории."""

    name = "static"

    def __init__(self, input_dir: Path | str) -> None:
        self.input_dir = Path(input_dir)

    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        objects: list[RawObject] = []
        if not self.input_dir.exists():
            logger.warning("Директория не найдена: %s", self.input_dir)
            return objects

        for path in sorted(self.input_dir.iterdir()):
            if path.suffix == ".json":
                objects.extend(self._load_json(path))
            elif path.suffix == ".csv":
                objects.extend(self._load_csv(path))

        logger.info("StaticLoader: загружено %d объектов из %s", len(objects), self.input_dir)
        return objects

    def _load_json(self, path: Path) -> list[RawObject]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [self._to_raw(item, path.name) for item in data]
            if isinstance(data, dict) and "objects" in data:
                return [self._to_raw(item, path.name) for item in data["objects"]]
        except Exception as e:
            logger.error("Ошибка чтения %s: %s", path, e)
        return []

    def _load_csv(self, path: Path) -> list[RawObject]:
        result = []
        try:
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    result.append(self._to_raw(row, path.name))
        except Exception as e:
            logger.error("Ошибка чтения %s: %s", path, e)
        return result

    @staticmethod
    def _to_raw(item: dict, filename: str) -> RawObject:
        def _float(val) -> float | None:
            if val is None or val == "":
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        return RawObject(
            source_name=str(item.get("source_name", f"static:{filename}")),
            raw_name=str(item.get("raw_name", item.get("name", ""))),
            raw_type=str(item.get("raw_type", item.get("type", ""))),
            raw_address=str(item.get("raw_address", item.get("address", ""))),
            raw_phone=str(item.get("raw_phone", item.get("phone", ""))),
            raw_website=str(item.get("raw_website", item.get("website", ""))),
            raw_work_time=str(item.get("raw_work_time", item.get("work_time", ""))),
            raw_description=str(item.get("raw_description", item.get("description", ""))),
            raw_lat=_float(item.get("raw_lat", item.get("lat"))),
            raw_lon=_float(item.get("raw_lon", item.get("lon"))),
            raw_category=str(item.get("raw_category", item.get("category", ""))),
            raw_subcategory=str(item.get("raw_subcategory", item.get("subcategory", ""))),
        )
