"""Почему: единая точка доступа к БД, чтобы обеспечивать консистентность сессий."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Базовый класс моделей."""


_connect_args = {"timeout": 10} if settings.database_url.startswith("sqlite+") else {}

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
    """Почему: уменьшаем риск деградации SQLite при долгой работе и росте данных."""
    if not settings.database_url.startswith("sqlite+"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.execute("PRAGMA temp_store=MEMORY;")
    cursor.execute("PRAGMA auto_vacuum=INCREMENTAL;")
    cursor.close()


SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер для сессии БД."""

    async with SessionFactory() as session:
        yield session
