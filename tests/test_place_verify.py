"""Тесты достоверности мест: извлечение URL, маркеры закрытия, паспорт свежести."""

from __future__ import annotations

from app.services.place_verify import _CLOSED_MARKERS, _first_url


def test_first_url_extracts_from_mixed_source() -> None:
    assert _first_url("https://pochta.ru/offices/142718 ; Yandex Maps") == \
        "https://pochta.ru/offices/142718"
    assert _first_url("Yandex Maps / 2GIS (Измайлово, 12А)") is None
    assert _first_url(None) is None


def test_closed_markers_match_real_phrases() -> None:
    page = "<div>Организация закрыта. Больше не работает по этому адресу</div>".lower()
    assert any(m in page for m in _CLOSED_MARKERS)
    ok_page = "<div>Открыто до 21:00. Ежедневно</div>".lower()
    assert not any(m in ok_page for m in _CLOSED_MARKERS)


def test_post_office_142718_marked_inactive_in_seed() -> None:
    """Регрессия: закрытая почта в Измайлово не должна вернуться активной."""
    import json
    from pathlib import Path

    seed = json.loads(Path("data/places_seed.json").read_text(encoding="utf-8"))
    places = seed if isinstance(seed, list) else seed.get("places", seed)
    post = [p for p in places if p.get("name") == "Почта России 142718"]
    assert post, "запись о 142718 должна остаться в seed (с пометкой закрыто)"
    assert post[0]["is_active"] is False
    assert "закрыто" in post[0]["description"].lower()


def test_kb_post_office_points_to_lopatino() -> None:
    """Ответ про почту ведёт в работающее отделение, а не в закрытое."""
    from app.services.resident_kb import load_resident_kb

    load_resident_kb.cache_clear()
    entries = load_resident_kb()
    post = next(e for e in entries if e.id == "post_office")
    assert "Лопатино" in post.answer
    assert "закрыто" in post.answer.lower()
    assert post.verified_at == "2026-07-10"


def test_seed_places_have_verification_passport() -> None:
    """У всех мест в seed есть дата проверки."""
    import json
    from pathlib import Path

    seed = json.loads(Path("data/places_seed.json").read_text(encoding="utf-8"))
    places = seed if isinstance(seed, list) else seed.get("places", seed)
    missing = [p["name"] for p in places if not p.get("verified_at")]
    assert not missing, f"места без verified_at: {missing[:5]}"


def test_kb_directory_entries_defer_to_places() -> None:
    """Единый источник: KB-справочники не несут конфликтующих volatile-адресов.

    Раньше transport_metro утверждал «прямого автобуса нет», перебивая по
    приоритету свежую таблицу мест (маршрутка 1224к есть).
    """
    from app.services.resident_kb import load_resident_kb

    load_resident_kb.cache_clear()
    entries = {e.id: e for e in load_resident_kb()}

    tm = entries["transport_metro"]
    assert "прямого автобуса" not in tm.answer.lower()
    assert "1224" in tm.answer  # актуальный прямой маршрут
    # Банки указывают на ближайший банкомат (~2 км), а не только Видное 5-7 км
    assert "Аструм" in entries["banks"].answer
    # Свежесть проставлена
    for eid in ("transport_metro", "banks", "pharmacy", "sports_fitness", "shops_grocery"):
        assert entries[eid].verified_at == "2026-07-12", eid
