"""Почему: антифлуд должен работать быстро без лишних запросов в БД."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


class FloodTracker:
    """Простой трекер сообщений за окно времени."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = timedelta(seconds=window_seconds)
        self._messages: dict[tuple[int, int], deque[datetime]] = {}

    def register(self, user_id: int, chat_id: int, timestamp: datetime) -> int:
        key = (user_id, chat_id)
        if key not in self._messages:
            self._messages[key] = deque()
        bucket = self._messages[key]
        bucket.append(timestamp)
        cutoff = timestamp - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket)

    def cleanup(self) -> int:
        """Удаляет устаревшие записи из трекера. Возвращает количество удалённых."""
        now = datetime.now()
        cutoff = now - self.window
        stale_keys = [
            key for key, bucket in self._messages.items()
            if not bucket or bucket[-1] < cutoff
        ]
        for key in stale_keys:
            del self._messages[key]
        return len(stale_keys)
