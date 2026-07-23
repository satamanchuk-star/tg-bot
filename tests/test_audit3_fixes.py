"""Регрессии аудита-3: дозаполнение данных мест доезжает до живой БД."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Place


def test_seed_updates_contact_fields_of_existing_places(monkeypatch) -> None:
    """Телефон/часы, дозаполненные в places_seed.json, обновляют существующую запись.

    Раньше синхронизировались только is_active/verified_at — дозаполненные
    контактные данные для уже посеянных мест молча терялись.
    """
    from scripts import seed_places as sp

    key = {"name": "Аптека Тест", "address": "ул. Тестовая, 1", "category": "medical"}

    async def scenario() -> Place:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # Первый посев: без телефона и часов
        monkeypatch.setattr(sp, "_load_seed_data", lambda: [dict(key)])
        async with session_factory() as session:
            added = await sp.seed_places(session)
            await session.commit()
        assert added == 1

        # Второй посев: та же запись, но с дозаполненными полями
        monkeypatch.setattr(sp, "_load_seed_data", lambda: [
            dict(key, phone="+7 495 000-00-00", work_time="ежедневно 9:00-21:00",
                 description="круглосуточная выдача заказов")
        ])
        async with session_factory() as session:
            added = await sp.seed_places(session)
            await session.commit()
        assert added == 0  # не дубль, а обновление

        async with session_factory() as session:
            place = (await session.execute(select(Place))).scalars().one()
        await engine.dispose()
        return place

    place = asyncio.run(scenario())
    assert place.phone == "+7 495 000-00-00"
    assert place.work_time == "ежедневно 9:00-21:00"
    assert place.description == "круглосуточная выдача заказов"
