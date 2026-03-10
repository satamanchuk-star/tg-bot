"""Базовый интерфейс провайдера данных."""

from __future__ import annotations

import abc

from ..models import RawObject


class BaseProvider(abc.ABC):
    """Абстрактный провайдер инфраструктурных данных."""

    name: str = "base"

    @abc.abstractmethod
    def fetch(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
    ) -> list[RawObject]:
        """Получить сырые объекты в заданном радиусе."""
        ...
