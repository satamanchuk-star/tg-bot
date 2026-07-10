"""Тесты ночного бэкапа БД."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock

from app.services.backup import _sqlite_path_from_url, send_db_backup


def test_sqlite_path_from_url() -> None:
    assert str(_sqlite_path_from_url("sqlite+aiosqlite:///app/data/bot.db")) == "app/data/bot.db"
    assert str(_sqlite_path_from_url("sqlite:///data/x.db")) == "data/x.db"
    assert _sqlite_path_from_url("postgresql://user@host/db") is None


def test_send_db_backup_sends_document(tmp_path, monkeypatch) -> None:
    # Готовим маленькую живую SQLite-базу
    db_file = tmp_path / "bot.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('data')")

    from app.config import settings
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    bot = AsyncMock()
    asyncio.run(send_db_backup(bot))

    assert bot.send_document.await_count == 1
    args, kwargs = bot.send_document.await_args
    assert args[0] == settings.admin_log_chat_id
    assert "бэкап" in kwargs.get("caption", "").lower()


def test_send_db_backup_skips_non_sqlite(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "database_url", "postgresql://u@h/db")

    bot = AsyncMock()
    asyncio.run(send_db_backup(bot))
    assert bot.send_document.await_count == 0
