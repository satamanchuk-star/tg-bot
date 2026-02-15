"""Почему: фиксируем критичные регрессии, которые ломают запуск приложения."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.services import quiz_loader


def test_admin_module_importable() -> None:
    module = importlib.import_module("app.handlers.admin")
    assert module is not None


def test_sync_questions_from_xlsx_returns_zero_on_invalid_file(tmp_path: Path) -> None:
    async def _run() -> None:
        original_path = quiz_loader.QUIZ_XLSX_PATH
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            quiz_loader.QUIZ_XLSX_PATH = tmp_path / "broken.xlsx"
            quiz_loader.QUIZ_XLSX_PATH.write_text("not a zip", encoding="utf-8")

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            async with session_factory() as session:
                total, unique = await quiz_loader.sync_questions_from_xlsx(session)
                assert (total, unique) == (0, 0)
        finally:
            await engine.dispose()
            quiz_loader.QUIZ_XLSX_PATH = original_path

    asyncio.run(_run())
