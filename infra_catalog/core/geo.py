"""Геолокационные утилиты."""

import math


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками (км) по формуле Haversine."""
    R = 6371.0
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def is_within_radius(
    lat: float, lon: float,
    center_lat: float, center_lon: float,
    radius_km: float,
) -> bool:
    return haversine_km(lat, lon, center_lat, center_lon) <= radius_km
