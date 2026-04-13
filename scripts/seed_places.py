"""Почему: заполняем таблицу places начальными данными инфраструктуры, чтобы бот отвечал без Google Sheets."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Place

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "places_seed.json"
# Fallback: bind mount data/ может перекрывать файл, но kb/ всегда доступен в образе
SEED_FILE_FALLBACK = Path(__file__).resolve().parent.parent / "kb" / "places_seed.json"


def _load_seed_data() -> list[dict[str, object]]:
    path = SEED_FILE
    if not path.exists():
        path = SEED_FILE_FALLBACK
    if not path.exists():
        logger.warning("Файл %s не найден, seed пропущен.", path)
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def purge_old_places(session: AsyncSession) -> int:
    """Удаляет записи из places, которых нет в актуальном seed-файле. Возвращает количество удалённых."""
    data = _load_seed_data()
    if not data:
        return 0

    # Собираем набор (name, address, category) из seed-файла
    seed_keys: set[tuple[str, str, str]] = set()
    for item in data:
        name = item.get("name")
        address = item.get("address")
        category = item.get("category")
        if all((name, address, category)):
            seed_keys.add((name, address, category))

    # Загружаем все записи из БД
    rows = (await session.execute(select(Place))).scalars().all()
    ids_to_delete: list[int] = []
    for row in rows:
        if (row.name, row.address, row.category) not in seed_keys:
            ids_to_delete.append(row.id)

    if ids_to_delete:
        await session.execute(delete(Place).where(Place.id.in_(ids_to_delete)))
        await session.flush()
        logger.info("Удалено %s устаревших записей инфраструктуры.", len(ids_to_delete))

    return len(ids_to_delete)


async def seed_places(session: AsyncSession) -> int:
    """Добавляет seed-записи в таблицу places, если их ещё нет. Возвращает количество добавленных."""
    data = _load_seed_data()
    if not data:
        return 0

    # Один запрос вместо N+1: загружаем все существующие ключи разом
    existing_rows = (
        await session.execute(select(Place.name, Place.address, Place.category))
    ).all()
    existing_keys = {(row.name, row.address, row.category) for row in existing_rows}

    added = 0
    for item in data:
        name = item.get("name")
        address = item.get("address")
        category = item.get("category")
        if not all((name, address, category)):
            continue
        if (name, address, category) in existing_keys:
            continue

        place = Place(
            name=name,
            category=category,
            subcategory=item.get("subcategory"),
            address=address,
            phone=item.get("phone"),
            website=item.get("website"),
            work_time=item.get("work_time"),
            description=item.get("description"),
            lat=item.get("lat"),
            lon=item.get("lon"),
            distance_km=item.get("distance_km"),
            source=item.get("source"),
            is_active=item.get("is_active", True),
        )
        session.add(place)
        added += 1

    if added:
        await session.flush()

    return added
