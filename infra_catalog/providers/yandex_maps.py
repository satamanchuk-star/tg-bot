"""Провайдер: Яндекс Карты.

TODO: Реализовать при наличии API-ключа Яндекс.Карт (Geocoder / Places API).
Текущая версия — заглушка, возвращает пустой список.
"""

from __future__ import annotations

import logging

from ..models import RawObject
from .base import BaseProvider

logger = logging.getLogger(__name__)


class YandexMapsProvider(BaseProvider):
    name = "yandex_maps"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        if not self.api_key:
            logger.info("YandexMapsProvider: API-ключ не задан, пропуск")
            return []
        # TODO: реализовать запросы к API
        logger.warning("YandexMapsProvider: сбор данных не реализован")
        return []
