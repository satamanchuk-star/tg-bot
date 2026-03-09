from scripts.import_places_from_google_sheets import _map_columns, _row_to_payload


def test_map_columns_uses_headers_by_name() -> None:
    headers = ["Название", "Категория", "Адрес", "Расстояние (км)", "Широта", "Долгота"]

    mapped = _map_columns(headers)

    assert mapped["name"] == "Название"
    assert mapped["category"] == "Категория"
    assert mapped["address"] == "Адрес"
    assert mapped["distance_km"] == "Расстояние (км)"


def test_row_payload_normalizes_and_casts_values() -> None:
    row = {
        "Название": " Поликлиника №1 ",
        "Категория": " Медицинские учреждения ",
        "Адрес": " ул. Лесная, 10 ",
        "Расстояние (км)": "1,5",
        "Широта": "55.123",
        "Долгота": "37.987",
        "Активен": "да",
    }
    mapping = {
        "name": "Название",
        "category": "Категория",
        "address": "Адрес",
        "distance_km": "Расстояние (км)",
        "lat": "Широта",
        "lon": "Долгота",
        "is_active": "Активен",
    }

    payload = _row_to_payload(row, mapping)

    assert payload["name"] == "Поликлиника №1"
    assert payload["category"] == "Медицинские учреждения"
    assert payload["address"] == "ул. Лесная, 10"
    assert payload["distance_km"] == 1.5
    assert payload["lat"] == 55.123
    assert payload["lon"] == 37.987
    assert payload["is_active"] is True
