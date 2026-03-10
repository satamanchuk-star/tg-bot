"""Тесты дедупликации."""

from infra_catalog.models import InfraObject
from infra_catalog.core.deduplicator import deduplicate


def _make_obj(name: str, address: str, lat: float = 55.55, lon: float = 37.62,
              source: str = "test", phone: str | None = None) -> InfraObject:
    return InfraObject(
        name=name, category="medical", subcategory="clinic",
        address=address, lat=lat, lon=lon, distance_km=1.0,
        source=source, phone=phone,
    )


def test_dedup_exact_match():
    objs = [
        _make_obj("Клиника А", "ул. Тестовая, д. 1", phone="+71111111111"),
        _make_obj("Клиника А", "ул. Тестовая, д. 1", phone="+72222222222"),
    ]
    result, issues = deduplicate(objs)
    assert len(result) == 1
    # Телефоны объединены
    assert result[0].phone and ";" in result[0].phone


def test_dedup_no_duplicates():
    objs = [
        _make_obj("Клиника А", "ул. Тестовая, д. 1", lat=55.55, lon=37.62),
        _make_obj("Клиника Б", "ул. Другая, д. 2", lat=55.60, lon=37.70),
    ]
    result, issues = deduplicate(objs)
    assert len(result) == 2


def test_dedup_proximity():
    objs = [
        _make_obj("Клиника А", "адрес 1", lat=55.5500, lon=37.6200),
        _make_obj("Клиника А", "адрес 2", lat=55.5501, lon=37.6201),
    ]
    result, issues = deduplicate(objs)
    assert len(result) == 1


def test_dedup_different_categories_not_merged():
    obj1 = InfraObject(
        name="Объект", category="medical", subcategory="clinic",
        address="адрес", lat=55.55, lon=37.62, distance_km=1.0, source="t",
    )
    obj2 = InfraObject(
        name="Объект", category="food", subcategory="cafe",
        address="адрес другой", lat=55.5501, lon=37.6201, distance_km=1.0, source="t",
    )
    result, _ = deduplicate([obj1, obj2])
    assert len(result) == 2
