"""Провайдер: официальные сайты организаций.

TODO: Реализовать парсеры конкретных сайтов по необходимости.
"""

from __future__ import annotations

import logging

from ..models import RawObject
from .base import BaseProvider

logger = logging.getLogger(__name__)


class OfficialSitesProvider(BaseProvider):
    name = "official"

    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        logger.info("OfficialSitesProvider: не реализован, пропуск")
        return []
