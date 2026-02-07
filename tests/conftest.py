"""Почему: делаем локальный запуск pytest автономным без ручной настройки окружения."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("FORUM_CHAT_ID", "1")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
