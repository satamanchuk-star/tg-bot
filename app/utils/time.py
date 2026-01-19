"""Почему: единое место для работы с временем и таймзоной."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings


def now_tz() -> datetime:
    return datetime.now(tz=ZoneInfo(settings.timezone))
