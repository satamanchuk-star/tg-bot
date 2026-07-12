"""Тесты поиска по справочнику мест, включая кириллический регистр."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base


@pytest.fixture()
def seeded_places(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async for s in _get_session():
            from scripts.seed_places import seed_places
            await seed_places(s)
            await s.commit()
            break

    async def _get_session():
        async with factory() as session:
            yield session

    monkeypatch.setattr("app.services.ai_module.get_session", _get_session)
    asyncio.run(_prepare())
    yield
    asyncio.run(engine.dispose())


def _search(query: str) -> str:
    from app.services.ai_module import _get_places_context
    return asyncio.run(_get_places_context(query))


def test_search_finds_uppercase_cyrillic_azs(seeded_places) -> None:
    """Регресс: «АЗС» (заглавная кириллица) находится по запросу «азс».

    SQLite ILIKE регистронезависим только для латиницы, поэтому поиск
    выполняется в Python.
    """
    ctx = _search("где заправка азс")
    assert "АЗС" in ctx


def test_search_finds_new_categories(seeded_places) -> None:
    """Новые категории ищутся по бытовым формулировкам."""
    assert "банкомат" in _search("где ближайший банкомат").lower()
    assert "метро" in _search("как доехать до метро").lower()
    assert "ozon" in _search("пункт выдачи озон").lower() or "пвз" in _search("пункт выдачи озон").lower()
    assert "ветклиника" in _search("где ветклиника").lower()


def test_search_post_office_points_to_working_one(seeded_places) -> None:
    """«Почта» не должна выдавать закрытое отделение 142718 (Измайлово)."""
    ctx = _search("ближайшая почта")
    assert "142718" not in ctx  # закрытое отделение отфильтровано (is_active=false)


def test_search_natural_phrasing_via_synonyms(seeded_places) -> None:
    """Бытовые формулировки без ключевого слова карточки (заправка→АЗС и др.)."""
    assert "АЗС" in _search("где заправка")          # не только «азс»
    assert "анкомат" in _search("нужен банкомат")     # Банкомат/банкомат
    assert "аршрутка" in _search("как доехать в москву") or "етро" in _search("как доехать в москву")
    assert "ВетЛис" in _search("ветеринар рядом")
    assert "равмпункт" in _search("травмпункт где")


def test_route_query_keeps_destination(seeded_places) -> None:
    """«Как доехать до X» должно вести к X, а не только к транспорту."""
    assert "Почта" in _search("как доехать до почты")
    assert "документы" in _search("как доехать до мфц").lower()
    # «доехать в москву» без явного назначения-места → транспорт, это ок
    assert "етро" in _search("как доехать в москву") or "аршрутка" in _search("как доехать в москву")


def test_no_noise_for_missing_categories(seeded_places) -> None:
    """Нет записей автосервиса → пустой контекст, а не автобусы/мусор."""
    assert _search("автосервис рядом") == ""
    assert _search("где шиномонтаж") == ""
