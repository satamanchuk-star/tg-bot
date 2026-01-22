"""Почему: единое место для работы с временем и таймзоной."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings


def now_tz() -> datetime:
    return datetime.now(tz=ZoneInfo(settings.timezone))


def is_game_time_allowed(start_hour: int, end_hour: int) -> bool:
    """Проверяет, что текущее время МСК в диапазоне [start_hour, end_hour)."""
    current = now_tz()
    return start_hour <= current.hour < end_hour
