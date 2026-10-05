"""Приписка о давности данных в ответах ассистента.

Повод: график УК поменялся, а бот полгода отдавал старый. На октябрь 2026
у 47 из 57 записей базы знаний последняя проверка была в марте.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.services import freshness as f

TODAY = date(2026, 10, 5)


def _note(reply: str, kb: dict, places: dict | None = None) -> str | None:
    """Прогон freshness_note на подставных реестрах фактов."""
    async def _places():
        return dict(places or {})

    orig_kb, orig_places = f._kb_registry, f._places_registry
    f._kb_registry = lambda: dict(kb)
    f._places_registry = _places
    try:
        return asyncio.run(f.freshness_note(reply, today=TODAY))
    finally:
        f._kb_registry, f._places_registry = orig_kb, orig_places


# --- Извлечение фактов ---

def test_phones_normalized_and_short_numbers_ignored() -> None:
    facts = f._facts("УК +7 (495) 401-60-06, то же 8 495 401 60 06; экстренно 112, Сбер 900")
    assert facts == {"tel:4954016006"}          # разные записи номера — один факт


def test_hour_ranges_normalized() -> None:
    assert f._facts("09:00–18:00") == f._facts("с 9.00 - 18.00") == {"hrs:9:00-18:00"}
    # Одиночное время ничего не идентифицирует и фактом не считается.
    assert f._facts("обед в 13:00") == set()


# --- Решение о приписке ---

def test_stale_fact_gets_note_with_month() -> None:
    note = _note("Лифтек: 8 (903) 779-11-63", {"tel:9037791163": date(2026, 3, 16)})
    assert note and "проверены в марте 2026" in note and "⚠️ Устарело" in note


def test_fresh_fact_gets_no_note() -> None:
    assert _note("УК: +7 (495) 401-60-06", {"tel:4954016006": date(2026, 10, 5)}) is None


def test_fact_confirmed_anywhere_counts_as_fresh() -> None:
    """Телефон в старой записи, но подтверждён карточкой места — свежий."""
    note = _note(
        "Звоните 8 (903) 779-11-63",
        kb={"tel:9037791163": date(2026, 3, 16)},
        places={"tel:9037791163": date(2026, 9, 1)},
    )
    assert note is None


def test_unknown_phone_is_not_dated() -> None:
    """Номер не из наших данных (например, из веб-поиска) — не датируем."""
    assert _note("Позвоните +7 916 000-00-00", {"tel:4954016006": date(2026, 3, 1)}) is None


def test_mixed_reply_dated_by_oldest_fact() -> None:
    note = _note(
        "УК +7 (495) 401-60-06, лифт 8 (903) 779-11-63",
        {"tel:4954016006": date(2026, 10, 5), "tel:9037791163": date(2025, 11, 2)},
    )
    assert note and "в ноябре 2025" in note


def test_never_verified_fact_says_so() -> None:
    note = _note("Шиномонтаж +7 495 147-26-22", {}, places={"tel:4951472622": date.min})
    assert note and "давно не перепроверялись" in note


def test_reply_without_facts_untouched() -> None:
    assert _note("Привет, сосед!", {"tel:4954016006": date(2026, 1, 1)}) is None


def test_places_failure_keeps_kb_note() -> None:
    """Карточки мест недоступны (БД упала) — приписка по базе знаний остаётся."""
    async def _boom():
        raise RuntimeError("база недоступна")

    orig_kb, orig_places = f._kb_registry, f._places_registry
    f._kb_registry = lambda: {"tel:9037791163": date(2026, 3, 16)}
    f._places_registry = _boom
    try:
        note = asyncio.run(f.freshness_note("Лифтек 8 (903) 779-11-63", today=TODAY))
    finally:
        f._kb_registry, f._places_registry = orig_kb, orig_places
    assert note and "марте 2026" in note


def test_with_freshness_note_never_breaks_reply(monkeypatch) -> None:
    async def _boom(reply, **kwargs):
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(f, "freshness_note", _boom)
    assert asyncio.run(f.with_freshness_note("ответ")) == "ответ"


def test_with_freshness_note_appends_after_blank_line(monkeypatch) -> None:
    async def _fixed(reply, **kwargs):
        return "ℹ️ приписка"

    monkeypatch.setattr(f, "freshness_note", _fixed)
    assert asyncio.run(f.with_freshness_note("ответ")) == "ответ\n\nℹ️ приписка"


# --- Реальная база знаний ---

@pytest.mark.parametrize(("reply", "expect_note"), [
    ("Лифт: Лифтек 8 (903) 779-11-63", True),         # с марта не проверяли
    ("УК: +7 (495) 401-60-06", False),                # подтверждено объявлением УК
    ("Аварийка: +7 (495) 085-33-30", False),          # подтверждено объявлением УК
    ("Колл-центр УК: +7 (495) 182-72-72", True),      # в объявлении его нет
    ("УК принимает Вт–Чт 09:00–18:00", False),        # новый график
])
def test_real_kb(monkeypatch, reply: str, expect_note: bool) -> None:
    from app.services.resident_kb import load_resident_kb

    async def _no_places():
        return {}

    load_resident_kb.cache_clear()
    monkeypatch.setattr(f, "_places_registry", _no_places)
    note = asyncio.run(f.freshness_note(reply, today=TODAY))
    assert bool(note) is expect_note, note


def test_partially_confirmed_entries_keep_old_verified_at() -> None:
    """verified_at поднимается, только если перепроверены ВСЕ факты записи.

    Сводка контактов и контакты УК содержат номера, которых нет в объявлении
    УК (колл-центр, Лифтек, участковый...). Свежая дата у такой записи сделала
    бы все её телефоны «свежими» и отключила бы для них приписку.
    """
    from app.services.resident_kb import load_resident_kb

    load_resident_kb.cache_clear()
    by_id = {e.id: e for e in load_resident_kb()}
    assert by_id["useful_contacts_summary"].verified_at < "2026-10-01"
    assert by_id["uk_contacts"].verified_at < "2026-10-01"
    assert by_id["uk_schedule"].verified_at == "2026-10-05"


def test_both_reply_paths_add_the_note() -> None:
    """И /ai, и ответ на упоминание проходят через приписку."""
    import inspect

    from app.handlers import help as h

    assert inspect.getsource(h).count("await with_freshness_note(reply)") == 2
