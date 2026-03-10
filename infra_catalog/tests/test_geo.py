"""Тесты геолокационных утилит."""

import pytest
from infra_catalog.core.geo import haversine_km, is_within_radius


def test_haversine_same_point():
    assert haversine_km(55.5, 37.6, 55.5, 37.6) == 0.0


def test_haversine_known_distance():
    # Москва (55.7558, 37.6173) - Санкт-Петербург (59.9343, 30.3351) ≈ 634 км
    dist = haversine_km(55.7558, 37.6173, 59.9343, 30.3351)
    assert 630 < dist < 640


def test_haversine_short_distance():
    # Примерно 1 км
    dist = haversine_km(55.5, 37.6, 55.509, 37.6)
    assert 0.9 < dist < 1.1


def test_is_within_radius_true():
    assert is_within_radius(55.525, 37.616, 55.525238, 37.616287, 10.0)


def test_is_within_radius_false():
    # ~26 км от центра
    assert not is_within_radius(55.7558, 37.6173, 55.525238, 37.616287, 10.0)
