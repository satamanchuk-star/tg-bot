"""Почему: антифлуд должен работать быстро без лишних запросов в БД."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


class FloodTracker:
    """Простой трекер сообщений за окно времени."""

    # Каждые N регистраций прогоняемся по всем bucket'ам и удаляем записи
    # пользователей, которые давно не писали (иначе dict рос бы бесконечно).
    _SWEEP_EVERY = 500

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = timedelta(seconds=window_seconds)
        self._messages: dict[tuple[int, int], deque[datetime]] = {}
        self._calls_since_sweep = 0

    def register(self, user_id: int, chat_id: int, timestamp: datetime) -> int:
        key = (user_id, chat_id)
        if key not in self._messages:
            self._messages[key] = deque()
        bucket = self._messages[key]
        bucket.append(timestamp)
        cutoff = timestamp - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        count = len(bucket)

        self._calls_since_sweep += 1
        if self._calls_since_sweep >= self._SWEEP_EVERY:
            self._sweep(cutoff)
            self._calls_since_sweep = 0

        return count

    def _sweep(self, cutoff: datetime) -> None:
        """Удаляет bucket'ы пользователей, которые не писали в пределах окна."""
        stale_keys: list[tuple[int, int]] = []
        for key, bucket in self._messages.items():
            # Если последний таймстамп старше cutoff — пользователь уже давно молчит.
            if not bucket or bucket[-1] < cutoff:
                stale_keys.append(key)
        for key in stale_keys:
            self._messages.pop(key, None)
