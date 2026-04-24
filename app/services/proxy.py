"""Автоподбор рабочих прокси для Telegram Bot API.

Поддерживает HTTP/HTTPS/SOCKS4/SOCKS5 (через aiohttp_socks).
Хранит пул проверенных рабочих прокси, сохраняет между перезапусками.

ВАЖНО: Telegram Bot API работает по HTTPS, поэтому MTProto-прокси здесь
не подходят — они только для клиентов Telegram (Telegram app, Pyrogram,
Telethon).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)


_SOURCES: dict[str, tuple[str, ...]] = {
    "http": (
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    ),
    "socks4": (
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    ),
    "socks5": (
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    ),
}

_FETCH_TIMEOUT = 15.0
_TEST_TIMEOUT = 6.0
_CONCURRENCY = 30  # меньше параллельных соединений — меньше нагрузка на сервер
_SCAN_GLOBAL_TIMEOUT = 120.0


def _parse_proxy_line(line: str, scheme: str) -> Optional[str]:
    """`host:port` или `scheme://host:port` → нормализованный URL."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    if ":" not in line:
        return None
    return f"{scheme}://{line}"


@dataclass
class _Pool:
    working: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


class ProxyManager:
    """Управляет пулом прокси для Telegram Bot API.

    Публичный API:
      - `get_current()` / `rotate()` — отдают прокси из пула рабочих.
      - `refresh()` — тянет свежий список кандидатов из GitHub.
      - `find_working()` — пополняет пул рабочих (тестирует кандидатов).
      - `validate_working_pool()` — выбрасывает из пула отвалившиеся.
      - `refresh_and_find()` — цикл для планировщика.
    """

    def __init__(
        self,
        *,
        working_pool_size: int = 5,
        test_limit: int = 500,
        state_path: Optional[Path] = None,
        manual_proxy: Optional[str] = None,
    ) -> None:
        self._pool_size = max(1, working_pool_size)
        self._test_limit = max(50, test_limit)
        self._state_path = state_path
        self._manual_proxy = manual_proxy.strip() if manual_proxy else None
        self._pool = _Pool()
        self._index = 0
        self._lock = asyncio.Lock()
        if self._manual_proxy:
            logger.info("Прокси: используется ручной адрес из конфига")
            self._pool.working = [self._manual_proxy]
        else:
            self._load_state()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_current(self) -> Optional[str]:
        if not self._pool.working:
            return None
        return self._pool.working[self._index % len(self._pool.working)]

    def rotate(self) -> Optional[str]:
        if not self._pool.working:
            return None
        self._index = (self._index + 1) % len(self._pool.working)
        proxy = self._pool.working[self._index]
        logger.info("Прокси: ротация → %s", proxy)
        return proxy

    @property
    def count(self) -> int:
        return len(self._pool.working)

    async def refresh(self) -> int:
        if self._manual_proxy:
            return 0
        collected: set[str] = set()
        timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self._fetch_source(session, url, scheme)
                for scheme, urls in _SOURCES.items()
                for url in urls
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, set):
                collected |= r

        cands = list(collected)
        random.shuffle(cands)
        async with self._lock:
            working_set = set(self._pool.working)
            self._pool.candidates = [p for p in cands if p not in working_set]

        logger.info(
            "Прокси: загружено %d кандидатов, в пуле рабочих %d",
            len(self._pool.candidates),
            len(self._pool.working),
        )
        return len(self._pool.candidates)

    async def validate_working_pool(self) -> int:
        if self._manual_proxy:
            ok = await self._test_proxy(self._manual_proxy)
            if not ok:
                logger.warning("Прокси: ручной прокси %s не отвечает", self._manual_proxy)
            return 1 if ok else 0

        async with self._lock:
            to_test = list(self._pool.working)
        if not to_test:
            return 0

        results = await asyncio.gather(
            *(self._test_proxy(p) for p in to_test),
            return_exceptions=True,
        )
        alive = [p for p, ok in zip(to_test, results) if ok is True]
        async with self._lock:
            self._pool.working = alive
            if alive and self._index >= len(alive):
                self._index = 0

        logger.info(
            "Прокси: ревалидация пула — живых %d/%d", len(alive), len(to_test),
        )
        self._save_state()
        return len(alive)

    async def find_working(self, target: Optional[int] = None) -> int:
        if self._manual_proxy:
            return 0

        target = target or self._pool_size
        async with self._lock:
            remaining = max(0, target - len(self._pool.working))
            candidates = self._pool.candidates[: self._test_limit]

        if remaining == 0:
            return 0
        if not candidates:
            logger.warning("Прокси: нет кандидатов для проверки")
            return 0

        sem = asyncio.Semaphore(_CONCURRENCY)
        found: list[str] = []
        done = asyncio.Event()

        async def worker(proxy: str) -> None:
            if done.is_set():
                return
            async with sem:
                if done.is_set():
                    return
                if await self._test_proxy(proxy):
                    if not done.is_set():
                        found.append(proxy)
                        if len(found) >= remaining:
                            done.set()

        tasks = [asyncio.create_task(worker(p)) for p in candidates]

        async def cancel_on_done() -> None:
            await done.wait()
            for t in tasks:
                if not t.done():
                    t.cancel()

        canceller = asyncio.create_task(cancel_on_done())
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_SCAN_GLOBAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Прокси: глобальный таймаут поиска (%.0fs)", _SCAN_GLOBAL_TIMEOUT)
        finally:
            canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)

        async with self._lock:
            for p in found:
                if p not in self._pool.working:
                    self._pool.working.append(p)
            working_set = set(self._pool.working)
            self._pool.candidates = [
                p for p in self._pool.candidates if p not in working_set
            ]

        logger.info(
            "Прокси: найдено %d новых рабочих (проверено %d), всего в пуле %d",
            len(found),
            len(candidates),
            len(self._pool.working),
        )
        self._save_state()
        return len(found)

    async def refresh_and_find(self) -> None:
        await self.validate_working_pool()
        if self._manual_proxy:
            return
        await self.refresh()
        if len(self._pool.working) < self._pool_size:
            await self.find_working()

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_source(
        session: aiohttp.ClientSession, url: str, scheme: str,
    ) -> set[str]:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return set()
                text = await resp.text()
        except Exception as exc:
            logger.debug("Прокси: не удалось загрузить %s: %s", url, exc)
            return set()
        out: set[str] = set()
        for line in text.splitlines():
            parsed = _parse_proxy_line(line, scheme)
            if parsed:
                out.add(parsed)
        return out

    async def _test_proxy(self, proxy_url: str) -> bool:
        # Проверяем доступность api.telegram.org без токена — любой HTTP-ответ (200-499)
        # означает что прокси работает и Telegram отвечает. Используем HEAD чтобы не
        # тратить трафик и не триггерить rate-limit по токену.
        url = "https://api.telegram.org"
        try:
            connector = ProxyConnector.from_url(proxy_url)
        except Exception:
            return False
        timeout = aiohttp.ClientTimeout(total=_TEST_TIMEOUT)
        try:
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout,
            ) as session:
                async with session.head(url) as resp:
                    return 200 <= resp.status < 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Persistence: живые прокси выживают между перезапусками бота
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            working = data.get("working", [])
            if isinstance(working, list):
                self._pool.working = [
                    str(p) for p in working if isinstance(p, str)
                ]
                if self._pool.working:
                    logger.info(
                        "Прокси: восстановлено %d рабочих из %s",
                        len(self._pool.working),
                        self._state_path,
                    )
        except Exception as exc:
            logger.warning("Прокси: не удалось прочитать state: %s", exc)

    def _save_state(self) -> None:
        if not self._state_path or self._manual_proxy:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"working": self._pool.working},
                ensure_ascii=False,
                indent=2,
            )
            self._state_path.write_text(payload, encoding="utf-8")
        except Exception as exc:
            logger.warning("Прокси: не удалось сохранить state: %s", exc)
