"""Почему: отслеживание состояния бота хранится в БД и не зависит от памяти процесса."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HealthState


async def get_health_state(session: AsyncSession) -> HealthState:
    state = await session.get(HealthState, 1)
    if state is None:
        state = HealthState(id=1)
        session.add(state)
        await session.flush()
    return state


async def update_heartbeat(session: AsyncSession, timestamp: datetime) -> None:
    state = await get_health_state(session)
    state.last_heartbeat_at = timestamp
    await session.flush()


async def update_notice(session: AsyncSession, timestamp: datetime) -> None:
    state = await get_health_state(session)
    state.last_notice_at = timestamp
    await session.flush()
