"""Регрессии аудита-4: петля роста переживает рестарт, свежесть KB."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base


def test_pending_answer_survives_restart(monkeypatch) -> None:
    """Привязка «сообщение дайджеста → вопрос» живёт в БД, а не в памяти.

    Раньше рестарт бота между дайджестом и ответом админа молча рвал петлю:
    peek возвращал None и ответ не записывался в базу знаний.
    """

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _get_session():
            async with session_factory() as session:
                yield session

        from app.services import unanswered
        monkeypatch.setattr(unanswered, "get_session", _get_session)

        await unanswered.log_unanswered(100, "где ближайший каток?")
        from sqlalchemy import select
        from app.models import UnansweredQuestion
        async with session_factory() as session:
            q = (await session.execute(select(UnansweredQuestion))).scalars().one()
            qid = q.id

        # «Рестарт»: никакого состояния в памяти между register и peek
        await unanswered.register_pending_answer(777, qid)
        await unanswered.register_pending_answer(778, qid)  # приглашение

        found = await unanswered.peek_pending_answer(778)
        assert found == qid
        found_first = await unanswered.peek_pending_answer(777)
        assert found_first == qid
        assert await unanswered.peek_pending_answer(999) is None

        # Ответ админа закрывает вопрос → привязка больше не находится
        answered = await unanswered.save_admin_answer(qid, "Каток у Дворца спорта Видное", 42)
        assert answered is not None
        assert await unanswered.peek_pending_answer(778) is None

        await engine.dispose()

    asyncio.run(scenario())


def test_all_kb_entries_have_verified_at() -> None:
    """Симметрия свежести: у каждой записи KB есть паспорт verified_at."""
    from app.services.resident_kb import load_resident_kb

    missing = [e.id for e in load_resident_kb() if not getattr(e, "verified_at", None)]
    assert missing == []


def test_kb_urls_extracted_for_verification() -> None:
    """Сверка place_verify видит ссылки из ответов KB (сайт УК и т.п.)."""
    from app.services.place_verify import _kb_urls

    pairs = _kb_urls()
    assert pairs, "в KB есть ссылки (ukvek-sity.ru и др.) — список не должен быть пуст"
    assert all(url.startswith("http") for _, url in pairs)
    # Google-формы намеренно исключены
    assert not any("docs.google.com" in url for _, url in pairs)
