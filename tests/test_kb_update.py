"""Обновление базы знаний доезжает до ответов бота.

Повод: после смены графика УК бот продолжал отвечать по-старому. Старые
данные переживали выкладку в трёх местах: копия базы в data/ на сервере
(bind mount перекрывает файл из образа), закреплённые FAQ-ответы в БД и
история диалога, где бот сам называл старый график.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, FrequentQuestion
from app.services.faq import get_faq_answer, reset_faq_on_kb_change
from app.services.resident_kb import resolve_kb_path


# --- Какой файл базы читать ---

def _write_kb(path: Path, updated_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": "x", "updated_at": updated_at}]), encoding="utf-8")


def test_stale_server_copy_does_not_shadow_image(tmp_path: Path) -> None:
    """Старая копия в data/ не перекрывает свежий файл из образа."""
    _write_kb(tmp_path / "data" / "resident_kb.json", "2026-03-16")
    _write_kb(tmp_path / "kb" / "resident_kb.json", "2026-10-05")
    assert resolve_kb_path(tmp_path) == tmp_path / "kb" / "resident_kb.json"


def test_server_copy_wins_on_tie_or_when_newer(tmp_path: Path) -> None:
    """Ручная правка на сервере поверх той же версии — читаем её."""
    _write_kb(tmp_path / "kb" / "resident_kb.json", "2026-10-05")
    _write_kb(tmp_path / "data" / "resident_kb.json", "2026-10-05")
    assert resolve_kb_path(tmp_path) == tmp_path / "data" / "resident_kb.json"
    _write_kb(tmp_path / "data" / "resident_kb.json", "2026-11-01")
    assert resolve_kb_path(tmp_path) == tmp_path / "data" / "resident_kb.json"


def test_single_copy_is_used(tmp_path: Path) -> None:
    assert resolve_kb_path(tmp_path) is None
    _write_kb(tmp_path / "kb" / "resident_kb.json", "2026-10-05")
    assert resolve_kb_path(tmp_path) == tmp_path / "kb" / "resident_kb.json"


def test_broken_server_copy_loses(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "resident_kb.json").write_text("{битый", encoding="utf-8")
    _write_kb(tmp_path / "kb" / "resident_kb.json", "2026-10-05")
    assert resolve_kb_path(tmp_path) == tmp_path / "kb" / "resident_kb.json"


# --- Закреплённые FAQ-ответы ---

@pytest.fixture()
def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/faq.db")

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def test_kb_change_resets_locked_faq_once(factory) -> None:
    old = "График УК: Пн, Вт, Чт — 09:00–18:00, Ср — 09:00–19:00"

    async def run():
        async with factory() as session:
            session.add(FrequentQuestion(
                chat_id=1, question_key="график ук", best_answer=old,
                ask_count=5, positive_ratings=3, negative_ratings=0,
            ))
            await session.commit()
            # До выкладки ответ закреплён и подаётся модели.
            assert await get_faq_answer(session, chat_id=1, question_key="график ук") == old

            assert await reset_faq_on_kb_change(session, "aaaa") == 1
            assert await get_faq_answer(session, chat_id=1, question_key="график ук") is None
            fq = await session.get(FrequentQuestion, 1)
            assert fq.ask_count == 5               # статистика обращений сохранена
            assert fq.positive_ratings == 0

            # Тот же отпечаток (рестарт без новой базы) — ничего не трогаем.
            assert await reset_faq_on_kb_change(session, "aaaa") is None
            # Новая версия базы — снова сброс.
            assert await reset_faq_on_kb_change(session, "bbbb") == 0

    asyncio.run(run())


def test_empty_fingerprint_is_noop(factory) -> None:
    async def run():
        async with factory() as session:
            assert await reset_faq_on_kb_change(session, "") is None

    asyncio.run(run())


# --- История диалога и точки подключения ---

def test_prompt_prefers_kb_over_own_past_answers() -> None:
    from app.services.ai_module import get_static_assistant_prompt

    prompt = get_static_assistant_prompt()
    assert "данные обновились" in prompt and "не повторяй старое" in prompt


def test_startup_and_kb_reload_reset_faq() -> None:
    import inspect

    from app import main
    from app.handlers import admin

    assert "await _report_kb_update(bot)" in inspect.getsource(main.on_startup_warmup)
    assert "reset_faq_on_kb_change" in inspect.getsource(admin.kb_reload)


def test_real_kb_resolves_and_has_new_uk_schedule() -> None:
    from app.services.resident_kb import load_resident_kb

    load_resident_kb.cache_clear()
    by_id = {e.id: e for e in load_resident_kb()}
    assert "10:00–14:00" in by_id["uk_schedule"].answer      # суббота по новому графику


def test_startup_reports_new_kb_to_admins_once(factory, monkeypatch) -> None:
    """Новая база → одна строка в админ-чат; повторный рестарт — тишина."""
    from unittest.mock import AsyncMock

    from app import main

    async def _get_session():
        async with factory() as session:
            yield session

    monkeypatch.setattr(main, "get_session", _get_session)
    bot = AsyncMock()

    async def run():
        await main._report_kb_update(bot)
        await main._report_kb_update(bot)

    asyncio.run(run())
    assert bot.send_message.await_count == 1
    text = bot.send_message.await_args.args[1]
    assert "База знаний обновлена" in text and "Сброшено закреплённых FAQ-ответов: 0" in text
