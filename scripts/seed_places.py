"""Почему: заполняем таблицу places начальными данными инфраструктуры, чтобы бот отвечал без Google Sheets."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Place

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "places_seed.json"


def _load_seed_data() -> list[dict[str, object]]:
    if not SEED_FILE.exists():
        logger.warning("Файл %s не найден, seed пропущен.", SEED_FILE)
        return []
    with open(SEED_FILE, encoding="utf-8") as f:
        return json.load(f)


async def seed_places(session: AsyncSession) -> int:
    """Добавляет seed-записи в таблицу places, если их ещё нет. Возвращает количество добавленных."""
    data = _load_seed_data()
    if not data:
        return 0

    added = 0
    for item in data:
        name = item.get("name")
        address = item.get("address")
        category = item.get("category")
        if not all((name, address, category)):
            continue

        existing = (
            await session.execute(
                select(Place).where(
                    Place.name == name,
                    Place.address == address,
                    Place.category == category,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
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
            is_active=True,
        )
        session.add(place)
        added += 1

    if added:
        await session.flush()

    return added
