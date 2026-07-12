"""Тесты достоверности мест: извлечение URL, маркеры закрытия, паспорт свежести."""

from __future__ import annotations

import asyncio

from app.services.place_verify import _CLOSED_MARKERS, _CONCURRENCY, _first_url


def test_first_url_extracts_from_mixed_source() -> None:
    assert _first_url("https://pochta.ru/offices/142718 ; Yandex Maps") == \
        "https://pochta.ru/offices/142718"
    assert _first_url("Yandex Maps / 2GIS (Измайлово, 12А)") is None
    assert _first_url(None) is None


def test_verify_places_runs_concurrently(monkeypatch) -> None:
    """Сверка идёт пачками (Semaphore), а не строго последовательно.

    Регресс: раньше 100+ мест проверялись по одному с паузой 1с — минуты
    блокировки. Проверяем, что одновременно активно >1 проверки.
    """
    from types import SimpleNamespace

    from app.services import place_verify

    places = [
        SimpleNamespace(name=f"p{i}", category="c", source="https://ex/a", is_active=True)
        for i in range(_CONCURRENCY * 2)
    ]

    active = 0
    max_active = 0

    async def _fake_check(client, place):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return None

    async def _fake_session():
        class _Res:
            def scalars(self_inner):
                return SimpleNamespace(all=lambda: places)

        class _Sess:
            async def execute(self_inner, *a, **k):
                return _Res()

        yield _Sess()

    monkeypatch.setattr(place_verify, "_check_place", _fake_check)
    monkeypatch.setattr(place_verify, "get_session", _fake_session)

    bot = SimpleNamespace(
        send_message=lambda *a, **k: asyncio.sleep(0),
    )
    asyncio.run(place_verify.verify_places(bot))
    assert max_active > 1, "проверки должны идти параллельно"
    assert max_active <= _CONCURRENCY, "но не больше лимита семафора"


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


def test_directory_intents_removed_from_kb() -> None:
    """Единый источник: справочные записи (аптеки/банки/магазины/транспорт)
    удалены из KB — на «где аптека/банк» авторитетно отвечает таблица places,
    а не устаревший статичный текст KB (он перебивал места по приоритету).
    """
    from app.services.resident_kb import load_resident_kb

    load_resident_kb.cache_clear()
    ids = {e.id for e in load_resident_kb()}
    for gone in ("pharmacy", "banks", "shops_grocery", "sports_fitness",
                 "transport_metro", "transport_bus"):
        assert gone not in ids, f"{gone} должен отвечать из places, а не из KB"
