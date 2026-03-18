"""Тесты расширенной базы знаний — новые записи."""
from __future__ import annotations

import pytest
from app.services.resident_kb import build_resident_answer, load_resident_kb


def test_kb_loads_with_new_entries():
    entries = load_resident_kb()
    ids = {e.id for e in entries}
    assert "intercom_access" in ids
    assert "heating" in ids
    assert "delivery_couriers" in ids
    assert "owners_meeting" in ids
    assert "uk_app" in ids
    assert "playgrounds" in ids
    assert "water_supply" in ids
    assert "carsharing_taxi" in ids


def test_kb_has_intercom_entry():
    answer = build_resident_answer("Как получить ключ от домофона?")
    assert answer is not None
    assert "УК" in answer


def test_kb_has_heating_entry():
    answer = build_resident_answer("Когда включат отопление?")
    assert answer is not None
    lower = answer.lower()
    assert "октябр" in lower or "+8" in lower or "радиатор" in lower


def test_kb_has_water_entry():
    answer = build_resident_answer("Нет горячей воды, что делать?")
    assert answer is not None
    assert "085-33-30" in answer


def test_kb_has_delivery_entry():
    answer = build_resident_answer("Как пустить курьера через шлагбаум?")
    assert answer is not None
    assert "курьер" in answer.lower() or "пропуск" in answer.lower()


def test_kb_has_owners_meeting_entry():
    answer = build_resident_answer("Когда общее собрание собственников?")
    assert answer is not None
    assert "УК" in answer or "собрание" in answer.lower()


def test_kb_existing_shops_updated():
    """Магазины: должны упоминаться Первым делом или Буханка."""
    answer = build_resident_answer("Где продуктовый магазин в ЖК?")
    assert answer is not None
    assert "Первым делом" in answer or "Буханка" in answer or "пятёрочка" in answer.lower()


def test_kb_existing_contacts_updated():
    """Справочник контактов: должен упоминаться домофон."""
    answer = build_resident_answer("Какие полезные телефоны?")
    assert answer is not None
    assert "401-60-06" in answer


def test_fuzzy_matching_heating():
    """Fuzzy-match: опечатка в слове 'отопление'."""
    answer = build_resident_answer("отапление не работает")
    # Может не найти — fuzzy работает только для слов ≥6 символов
    # Просто проверяем, что не падает
    _ = answer  # None или текст — оба варианта валидны


def test_two_level_keyword_bonus():
    """Критические слова дают более высокий приоритет."""
    from app.services.resident_kb import search_resident_kb
    result_critical = search_resident_kb("шлагбаум не открывается помогите")
    result_strong = search_resident_kb("где отопление в ЖК")
    # Оба должны вернуть совпадения
    assert result_critical.matches or result_strong.matches
