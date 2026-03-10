"""Провайдер: Google Maps / Places API.

TODO: Реализовать при наличии API-ключа Google Places.
"""

from __future__ import annotations

import logging

from ..models import RawObject
from .base import BaseProvider

logger = logging.getLogger(__name__)


class GoogleMapsProvider(BaseProvider):
    name = "google_maps"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        if not self.api_key:
            logger.info("GoogleMapsProvider: API-ключ не задан, пропуск")
            return []
        logger.warning("GoogleMapsProvider: сбор данных не реализован")
        return []
