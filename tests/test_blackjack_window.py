"""Тест окна игры 22:00–00:00 МСК."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils import time as time_utils

_MSK = ZoneInfo("Europe/Moscow")


def _at(hour: int, minute: int, monkeypatch) -> bool:
    monkeypatch.setattr(
        time_utils, "now_tz",
        lambda: datetime(2026, 7, 13, hour, minute, tzinfo=_MSK),
    )
    return time_utils.is_game_time_allowed(22, 24)


def test_game_window_boundaries(monkeypatch) -> None:
    assert _at(21, 59, monkeypatch) is False
    assert _at(22, 0, monkeypatch) is True
    assert _at(23, 59, monkeypatch) is True
    assert _at(0, 0, monkeypatch) is False
    assert _at(12, 0, monkeypatch) is False
