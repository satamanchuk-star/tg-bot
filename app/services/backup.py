"""Почему: ночной бэкап SQLite в админ-чат — офсайт-копия на случай потери сервера.

Telegram хранит отправленные файлы бессрочно: восстановление = скачать документ
из лог-чата и положить на место data/bot.db.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import FSInputFile

from app.config import settings

logger = logging.getLogger(__name__)


def _sqlite_path_from_url(database_url: str) -> Path | None:
    """Извлекает путь к файлу из sqlite-URL; None для не-SQLite БД."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if database_url.startswith(prefix):
            return Path(database_url[len(prefix):])
    return None


def _make_backup_copy(src: Path, dst: Path) -> None:
    """Консистентный снимок SQLite через Online Backup API.

    Синхронно и с блокировкой — выполнять только через asyncio.to_thread,
    иначе event loop встанет на время копирования. Соединения закрываем явно
    в finally: контекст-менеджер sqlite3 коммитит, но не закрывает — иначе
    остаются висящие дескрипторы (утечка).
    """
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


async def send_db_backup(bot: Bot) -> None:
    """Отправляет консистентную копию БД документом в админ-чат (ночной job)."""
    src = _sqlite_path_from_url(settings.database_url)
    if src is None:
        logger.info("BACKUP: пропущен — база не SQLite (%s...)", settings.database_url[:20])
        return
    if not src.exists():
        logger.warning("BACKUP: файл БД не найден: %s", src)
        return

    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d_%H%M")
    tmp_path: Path | None = None
    try:
        # Консистентный снимок через SQLite Online Backup API — обычное копирование
        # файла могло бы поймать базу посреди записи. Само копирование —
        # блокирующее, поэтому уводим его в отдельный поток, чтобы не морозить loop.
        with tempfile.NamedTemporaryFile(
            prefix=f"bot_backup_{stamp}_", suffix=".db", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        await asyncio.to_thread(_make_backup_copy, src, tmp_path)

        size_mb = tmp_path.stat().st_size / (1024 * 1024)
        await bot.send_document(
            settings.admin_log_chat_id,
            FSInputFile(tmp_path, filename=f"bot_backup_{stamp}.db"),
            caption=(
                f"💾 Ночной бэкап БД ({size_mb:.1f} МБ)\n"
                "Восстановление: скачать файл → положить в data/bot.db → перезапустить бота."
            ),
            disable_notification=True,
        )
        logger.info("BACKUP: копия БД отправлена в админ-чат (%.1f МБ)", size_mb)
    except Exception:
        logger.warning("BACKUP: не удалось отправить копию БД.", exc_info=True)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
