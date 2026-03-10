"""Провайдер: 2GIS.

TODO: Реализовать при наличии API-ключа 2GIS.
"""

from __future__ import annotations

import logging

from ..models import RawObject
from .base import BaseProvider

logger = logging.getLogger(__name__)


class Gis2Provider(BaseProvider):
    name = "2gis"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        if not self.api_key:
            logger.info("Gis2Provider: API-ключ не задан, пропуск")
            return []
        logger.warning("Gis2Provider: сбор данных не реализован")
        return []
