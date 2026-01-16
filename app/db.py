"""Почему: единая точка доступа к БД, чтобы обеспечивать консистентность сессий."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Базовый класс моделей."""


engine: AsyncEngine = create_async_engine(settings.database_url, echo=False)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер для сессии БД."""

    async with SessionFactory() as session:
        yield session
