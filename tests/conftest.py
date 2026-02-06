"""Почему: тестам нужны базовые env-переменные для инициализации конфигурации приложения."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("FORUM_CHAT_ID", "-1001234567890")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-1001234567891")
