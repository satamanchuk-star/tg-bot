"""Почему: фиксируем критичные регрессии, которые ломают запуск приложения."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.main import heartbeat_job, on_startup
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


def test_on_startup_does_not_crash_when_cleanup_fails(monkeypatch) -> None:
    async def _run() -> None:
        bot = AsyncMock()

        async def _empty_async_gen():
            if False:
                yield

        monkeypatch.setattr("app.main.init_db", AsyncMock())
        monkeypatch.setattr(
            "app.main.cleanup_database",
            AsyncMock(side_effect=RuntimeError("cleanup failed")),
        )
        monkeypatch.setattr("app.main.get_session", _empty_async_gen)
        monkeypatch.setattr("app.main.heartbeat_job", AsyncMock())
        monkeypatch.setattr("app.main.get_ai_client", lambda: object())
        monkeypatch.setattr("app.main.set_ai_admin_notifier", lambda _fn: None)

        await on_startup(bot)

        assert bot.set_my_commands.call_count >= 1

    asyncio.run(_run())


def test_main_does_not_raise_when_polling_network_error(monkeypatch) -> None:
    async def _run() -> None:
        from app import main as main_module

        bot = AsyncMock()
        bot.session.close = AsyncMock()

        class DummyDispatcher:
            def __init__(self, *_args, **_kwargs) -> None:
                update_obj = type("UpdateObj", (), {})()
                update_obj.outer_middleware = lambda *_a, **_k: None
                error_obj = type("ErrorObj", (), {})()
                error_obj.register = lambda *_a, **_k: None
                self.update = update_obj
                self.error = error_obj

            def include_router(self, *_args, **_kwargs) -> None:
                return None

            async def start_polling(self, _bot) -> None:
                raise TelegramNetworkError(method="getMe", message="offline")

        monkeypatch.setattr(main_module, "STOP_FLAG", Path("/tmp/nonexistent-flag"))
        monkeypatch.setattr(main_module, "Bot", lambda *_a, **_k: bot)
        monkeypatch.setattr(main_module, "Dispatcher", DummyDispatcher)
        monkeypatch.setattr(main_module, "on_startup", AsyncMock())
        monkeypatch.setattr(main_module, "schedule_jobs", AsyncMock(return_value=None))
        monkeypatch.setattr(main_module, "close_ai_client", AsyncMock())

        await main_module.main()

        bot.session.close.assert_awaited_once()

    asyncio.run(_run())


def test_on_startup_does_not_crash_when_token_invalid(monkeypatch) -> None:
    async def _run() -> None:
        bot = AsyncMock()
        bot.get_me.side_effect = TelegramUnauthorizedError(
            method="getMe",
            message="invalid token",
        )

        async def _empty_async_gen():
            if False:
                yield

        monkeypatch.setattr("app.main.init_db", AsyncMock())
        monkeypatch.setattr("app.main.get_session", _empty_async_gen)
        monkeypatch.setattr("app.main.heartbeat_job", AsyncMock())
        monkeypatch.setattr("app.main.get_ai_client", lambda: object())
        monkeypatch.setattr("app.main.set_ai_admin_notifier", lambda _fn: None)

        await on_startup(bot)

        bot.set_my_commands.assert_not_called()
        bot.send_message.assert_not_called()

    asyncio.run(_run())


def test_main_does_not_raise_when_polling_api_error(monkeypatch) -> None:
    async def _run() -> None:
        from app import main as main_module

        bot = AsyncMock()
        bot.session.close = AsyncMock()

        class DummyDispatcher:
            def __init__(self, *_args, **_kwargs) -> None:
                update_obj = type("UpdateObj", (), {})()
                update_obj.outer_middleware = lambda *_a, **_k: None
                error_obj = type("ErrorObj", (), {})()
                error_obj.register = lambda *_a, **_k: None
                self.update = update_obj
                self.error = error_obj

            def include_router(self, *_args, **_kwargs) -> None:
                return None

            async def start_polling(self, _bot) -> None:
                raise TelegramUnauthorizedError(method="getMe", message="invalid token")

        monkeypatch.setattr(main_module, "STOP_FLAG", Path("/tmp/nonexistent-flag"))
        monkeypatch.setattr(main_module, "Bot", lambda *_a, **_k: bot)
        monkeypatch.setattr(main_module, "Dispatcher", DummyDispatcher)
        monkeypatch.setattr(main_module, "on_startup", AsyncMock())
        monkeypatch.setattr(main_module, "schedule_jobs", AsyncMock(return_value=None))
        monkeypatch.setattr(main_module, "close_ai_client", AsyncMock())

        await main_module.main()

        bot.session.close.assert_awaited_once()

    asyncio.run(_run())


def test_heartbeat_job_does_not_crash_when_telegram_unavailable(monkeypatch) -> None:
    async def _run() -> None:
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramNetworkError(
            method="sendMessage",
            message="offline",
        )

        class DummyState:
            def __init__(self) -> None:
                from datetime import datetime, timedelta, timezone

                self.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
                self.last_notice_at = None

        class DummySession:
            async def commit(self) -> None:
                return None

        session = DummySession()

        async def _session_gen():
            yield session

        monkeypatch.setattr("app.main.get_session", _session_gen)
        monkeypatch.setattr("app.main.get_health_state", AsyncMock(return_value=DummyState()))
        update_notice_mock = AsyncMock()
        monkeypatch.setattr("app.main.update_notice", update_notice_mock)
        monkeypatch.setattr("app.main.update_heartbeat", AsyncMock())

        await heartbeat_job(bot)

        bot.send_message.assert_awaited_once()
        update_notice_mock.assert_not_awaited()


    asyncio.run(_run())
