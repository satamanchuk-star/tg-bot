"""Почему: фиксируем критичные регрессии, которые ломают запуск приложения."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.main import on_startup
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


def test_on_startup_does_not_crash_when_telegram_unavailable(monkeypatch) -> None:
    async def _run() -> None:
        bot = AsyncMock()
        bot.get_me.side_effect = TelegramNetworkError(method="getMe", message="offline")

        async def _empty_async_gen():
            if False:
                yield

        monkeypatch.setattr("app.main.init_db", AsyncMock())
        monkeypatch.setattr("app.main.get_session", _empty_async_gen)
        monkeypatch.setattr("app.main.heartbeat_job", AsyncMock())
        monkeypatch.setattr("app.main.get_ai_client", lambda: object())
        monkeypatch.setattr("app.main.set_ai_admin_notifier", lambda _fn: None)

        await on_startup(bot)

        assert bot.get_me.await_count == 3
        bot.set_my_commands.assert_not_called()
        bot.send_message.assert_not_called()

    asyncio.run(_run())
