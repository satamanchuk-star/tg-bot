"""Тесты классификатора."""

from infra_catalog.core.classifier import classify


def test_pharmacy():
    assert classify("Аптека Горздрав") == ("medical", "pharmacy")


def test_dental():
    assert classify("Стоматология Улыбка") == ("medical", "dental")


def test_veterinary():
    assert classify("Ветклиника Айболит") == ("medical", "veterinary")


def test_cafe():
    assert classify("Кафе У дома") == ("food", "cafe")


def test_coffee_shop():
    assert classify("Кофейня Coffee Like") == ("food", "coffee_shop")


def test_school():
    assert classify("Школа №1") == ("education", "school")


def test_kindergarten():
    assert classify("Детский сад Солнышко") == ("education", "kindergarten")


def test_mfc():
    assert classify("МФЦ Видное") == ("mfc", "mfc_main")


def test_post():
    assert classify("Почта России 142700") == ("post", "russian_post")


def test_supermarket():
    cat, sub = classify("Пятёрочка")
    assert cat == "grocery"


def test_building_hypermarket():
    cat, sub = classify("Лемана ПРО")
    assert cat == "building_materials"
    assert sub == "building_hypermarket"


def test_unknown():
    assert classify("Неизвестный объект XYZ") == ("", "")


def test_raw_category_passthrough():
    assert classify("Что-то", raw_category="food", raw_subcategory="cafe") == ("food", "cafe")


def test_raw_category_invalid():
    # Невалидная пара — fallback на rules
    cat, sub = classify("Аптека", raw_category="invalid", raw_subcategory="invalid")
    assert cat == "medical"
    assert sub == "pharmacy"
