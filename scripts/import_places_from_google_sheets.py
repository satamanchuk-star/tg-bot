#!/usr/bin/env python3
"""Почему: загружаем справочник инфраструктуры из Google Sheets в локальную БД бота."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.config import settings
from app.db import SessionFactory
from app.models import Place

logger = logging.getLogger(__name__)

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "название", "объект"),
    "category": ("category", "категория", "основная категория"),
    "subcategory": ("subcategory", "подкатегория"),
    "address": ("address", "адрес"),
    "phone": ("phone", "телефон"),
    "website": ("website", "сайт", "url"),
    "lat": ("lat", "latitude", "широта"),
    "lon": ("lon", "lng", "longitude", "долгота"),
    "distance_km": ("distance_km", "distance", "расстояние", "расстояние (км)"),
    "source": ("source", "источник"),
    "work_time": ("work_time", "режим работы", "график"),
    "description": ("description", "описание", "комментарий"),
    "is_active": ("is_active", "active", "активен"),
}


@dataclass(slots=True)
class ImportStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


def _normalize_header(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").split())


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_float(value: str | None) -> float | None:
    raw = _clean_str(value)
    if raw is None:
        return None
    raw = raw.replace(",", ".")
    return float(raw)


def _parse_bool(value: str | None) -> bool:
    raw = (_clean_str(value) or "").lower()
    return raw not in {"", "0", "false", "нет", "no", "n"}


def _map_columns(headers: list[str]) -> dict[str, str]:
    normalized_headers = {_normalize_header(header): header for header in headers}
    mapped: dict[str, str] = {}

    for target_field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            source = normalized_headers.get(_normalize_header(alias))
            if source:
                mapped[target_field] = source
                break
    return mapped


def _row_to_payload(row: dict[str, str], column_map: dict[str, str]) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": _clean_str(row.get(column_map.get("name", ""))),
        "category": _clean_str(row.get(column_map.get("category", ""))),
        "address": _clean_str(row.get(column_map.get("address", ""))),
        "subcategory": _clean_str(row.get(column_map.get("subcategory", ""))),
        "phone": _clean_str(row.get(column_map.get("phone", ""))),
        "website": _clean_str(row.get(column_map.get("website", ""))),
        "source": _clean_str(row.get(column_map.get("source", ""))),
        "work_time": _clean_str(row.get(column_map.get("work_time", ""))),
        "description": _clean_str(row.get(column_map.get("description", ""))),
        "is_active": _parse_bool(row.get(column_map.get("is_active", ""))),
    }

    payload["lat"] = _parse_float(row.get(column_map.get("lat", "")))
    payload["lon"] = _parse_float(row.get(column_map.get("lon", "")))
    payload["distance_km"] = _parse_float(row.get(column_map.get("distance_km", "")))
    return payload


def _required_payload_valid(payload: dict[str, object]) -> bool:
    return all(payload.get(field) for field in ("name", "category", "address"))


def _load_rows() -> list[dict[str, str]]:
    import gspread
    from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

    if not settings.google_service_account_file:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_FILE не задан.")

    client = gspread.service_account(filename=settings.google_service_account_file)
    try:
        worksheet = client.open_by_key(settings.google_sheets_spreadsheet_id).worksheet(
            settings.google_sheets_worksheet_name
        )
    except SpreadsheetNotFound as exc:
        raise RuntimeError("Spreadsheet не найден или нет доступа сервисному аккаунту.") from exc
    except WorksheetNotFound as exc:
        raise RuntimeError("Лист Objects не найден в Google Sheets.") from exc
    except APIError as exc:
        raise RuntimeError(f"Ошибка Google Sheets API: {exc}") from exc

    rows = worksheet.get_all_records()
    return [{str(key): str(value) for key, value in row.items()} for row in rows]


async def run_import(*, dry_run: bool) -> ImportStats:
    logger.info("Старт импорта инфраструктуры из Google Sheets")
    rows = _load_rows()
    if not rows:
        logger.info("Импорт завершён: таблица пустая")
        return ImportStats()

    column_map = _map_columns(list(rows[0].keys()))
    for required in ("name", "category", "address"):
        if required not in column_map:
            raise RuntimeError(f"Не найдена обязательная колонка для поля '{required}'.")

    stats = ImportStats()
    async with SessionFactory() as session:
        for index, row in enumerate(rows, start=2):
            try:
                payload = _row_to_payload(row, column_map)
                if not _required_payload_valid(payload):
                    stats.skipped += 1
                    logger.warning("Строка %s пропущена: не заполнены обязательные поля.", index)
                    continue

                stmt = select(Place).where(
                    Place.name == payload["name"],
                    Place.address == payload["address"],
                    Place.category == payload["category"],
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing is None:
                    session.add(Place(**payload))
                    stats.created += 1
                else:
                    for key, value in payload.items():
                        setattr(existing, key, value)
                    existing.updated_at = datetime.utcnow()
                    stats.updated += 1
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                logger.exception("Ошибка обработки строки %s: %s", index, exc)

        if dry_run:
            await session.rollback()
            logger.info("Dry-run: изменения откатили")
        else:
            await session.commit()

    logger.info(
        "Импорт завершён: created=%s updated=%s skipped=%s errors=%s",
        stats.created,
        stats.updated,
        stats.skipped,
        stats.errors,
    )
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Импорт places из Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="Проверка без записи в БД")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args()
    asyncio.run(run_import(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
