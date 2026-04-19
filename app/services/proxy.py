"""Автоматический подбор и ротация прокси из публичных GitHub-списков."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Публичные репозитории с прокси, обновляются каждые 10–30 минут
_PROXY_SOURCES: tuple[str, ...] = (
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
)

_TEST_URL = "https://api.telegram.org"
_TEST_TIMEOUT = 6.0
_FETCH_TIMEOUT = 15.0
_CONCURRENCY = 30


class ProxyManager:
    """Хранит пул HTTP-прокси, умеет обновлять список и находить рабочий."""

    def __init__(self) -> None:
        self._proxies: list[str] = []
        self._index: int = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def refresh(self) -> int:
        """Загружает свежие прокси из всех источников. Возвращает кол-во."""
        collected: set[str] = set()
        timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                asyncio.create_task(self._fetch_source(session, url))
                for url in _PROXY_SOURCES
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, set):
                collected |= res

        proxies = list(collected)
        random.shuffle(proxies)
        async with self._lock:
            self._proxies = proxies
            self._index = 0

        logger.info("Прокси: загружено %d адресов из %d источников", len(proxies), len(_PROXY_SOURCES))
        return len(proxies)

    async def find_working(self, max_test: int = 150) -> bool:
        """Конкурентно проверяет первые max_test прокси, ставит индекс на первый рабочий."""
        async with self._lock:
            candidates = list(enumerate(self._proxies[:max_test]))

        if not candidates:
            logger.warning("Прокси: список пуст, нечего проверять")
            return False

        sem = asyncio.Semaphore(_CONCURRENCY)
        found_index: list[Optional[int]] = [None]
        stop_event = asyncio.Event()

        async def test_one(i: int, proxy: str) -> None:
            async with sem:
                if stop_event.is_set():
                    return
                ok = await self._test_proxy(proxy)
                if ok and not stop_event.is_set():
                    found_index[0] = i
                    stop_event.set()

        tasks = [asyncio.create_task(test_one(i, p)) for i, p in candidates]
        try:
            await asyncio.wait_for(
                asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED),
                timeout=_TEST_TIMEOUT * 2 + 5,
            )
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        idx = found_index[0]
        if idx is not None:
            async with self._lock:
                self._index = idx
            logger.info("Прокси: рабочий найден — %s", self._proxies[idx])
            return True

        logger.warning("Прокси: рабочий не найден среди %d проверенных", len(candidates))
        return False

    def get_current(self) -> Optional[str]:
        """Возвращает текущий прокси или None если список пуст."""
        if not self._proxies:
            return None
        return self._proxies[self._index]

    def rotate(self) -> Optional[str]:
        """Переходит к следующему прокси и возвращает его."""
        if not self._proxies:
            return None
        self._index = (self._index + 1) % len(self._proxies)
        proxy = self._proxies[self._index]
        logger.info("Прокси: ротация → %s", proxy)
        return proxy

    async def refresh_and_find(self) -> None:
        """Обновляет список и ищет рабочий — для вызова из планировщика."""
        count = await self.refresh()
        if count > 0:
            await self.find_working()

    @property
    def count(self) -> int:
        return len(self._proxies)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_source(session: aiohttp.ClientSession, url: str) -> set[str]:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return set()
                text = await resp.text()
        except Exception as exc:
            logger.debug("Прокси: не удалось загрузить %s: %s", url, exc)
            return set()

        result: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            if line and ":" in line and not line.startswith("#"):
                # Принимаем «host:port» и «http://host:port»
                if not line.startswith("http"):
                    line = f"http://{line}"
                result.add(line)
        return result

    @staticmethod
    async def _test_proxy(proxy: str) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=_TEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(_TEST_URL, proxy=proxy) as resp:
                    return resp.status < 500
        except Exception:
            return False
