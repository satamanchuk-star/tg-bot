"""Провайдер: региональные порталы (uslugi.mosreg.ru и т.п.).

TODO: Реализовать при необходимости.
"""

from __future__ import annotations

import logging

from ..models import RawObject
from .base import BaseProvider

logger = logging.getLogger(__name__)


class RegionalPortalsProvider(BaseProvider):
    name = "regional_portal"

    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        logger.info("RegionalPortalsProvider: не реализован, пропуск")
        return []
