"""Регрессии аудита-2: петля фидбека, «что умеешь», молчание без «?», бюджет контекста."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base


# ---------------------------------------------------------------------------
# Бюджет контекста знаний
# ---------------------------------------------------------------------------

def test_kb_budget_keeps_priority_order_and_drops_overflow() -> None:
    """Старшие источники входят целиком, младшие отбрасываются за бюджетом."""
    from app.services.ai_module import _apply_kb_budget

    blocks = [
        ("resident_canonical", "А" * 2000),
        ("rag", "Б" * 1500),
        ("faq", "В" * 1000),   # 2000+1500=3500, остаток 500 — блок режется
        ("places", "Г" * 500),  # бюджет исчерпан — отбрасывается
    ]
    result = _apply_kb_budget(blocks, budget=4000)
    joined = "\n".join(result)
    assert 'source="resident_canonical"' in joined
    assert 'source="rag"' in joined
    assert 'source="places"' not in joined
    # Суммарный контент не превышает бюджет (теги не считаем)
    content_len = sum(len(r) for r in result) - sum(
        len(f'<knowledge_base source="{s}">\n\n</knowledge_base>')
        for s in ("resident_canonical", "rag", "faq")
        if f'source="{s}"' in joined
    )
    assert content_len <= 4000


def test_kb_budget_cuts_on_line_boundary() -> None:
    """Обрезка по последней полной строке — без фактов, оборванных на полуслове."""
    from app.services.ai_module import _apply_kb_budget

    text = "Аптека: ул. Луговая 1, 9:00-21:00\nБанк: ул. Луговая 2, 10:00-18:00"
    result = _apply_kb_budget([("places", text)], budget=45)
    assert len(result) == 1
    assert "Аптека: ул. Луговая 1, 9:00-21:00" in result[0]
    assert "Банк" not in result[0]  # вторая строка не влезла целиком — отброшена


def test_kb_budget_skips_empty_blocks() -> None:
    from app.services.ai_module import _apply_kb_budget

    result = _apply_kb_budget([("rag", ""), ("faq", None), ("places", "Аптека рядом")])
    assert len(result) == 1
    assert 'source="places"' in result[0]


# ---------------------------------------------------------------------------
# Молчание без «?» — обращение к боту всегда получает ответ
# ---------------------------------------------------------------------------

def test_uncertain_reply_sent_even_without_question_mark() -> None:
    """«Жабот подскажи телефон УК» (без «?») больше не игнорируется молча."""
    from app.handlers import help as help_handler

    help_handler._LAST_UNCERTAIN_REPLY_TIME.clear()
    skipped = help_handler._should_skip_uncertain_reply(
        chat_id=1, user_id=2, thread_id=3,
        prompt="Жабот подскажи телефон УК",
        reply="Честно — не знаю, в моей базе этого нет.",
    )
    assert skipped is False


def test_uncertain_reply_cooldown_still_applies() -> None:
    """Повторный неинформативный ответ тому же жителю в той же теме — кулдаун."""
    from app.handlers import help as help_handler

    help_handler._LAST_UNCERTAIN_REPLY_TIME.clear()
    first = help_handler._should_skip_uncertain_reply(
        chat_id=1, user_id=2, thread_id=3,
        prompt="где каток?",
        reply="Честно — не знаю, в моей базе этого нет.",
    )
    second = help_handler._should_skip_uncertain_reply(
        chat_id=1, user_id=2, thread_id=3,
        prompt="ну где каток",
        reply="Честно — не знаю, в моей базе этого нет.",
    )
    assert first is False
    assert second is True


# ---------------------------------------------------------------------------
# «Что умеешь» — бот не отрицает игры, которые снова существуют
# ---------------------------------------------------------------------------

def test_abilities_context_mentions_games() -> None:
    from app.handlers.help import _ABILITIES_CONTEXT

    lowered = _ABILITIES_CONTEXT.lower()
    assert "викторина" in lowered
    assert "блэкджек" in lowered or "«21»" in _ABILITIES_CONTEXT
    assert "монет" in lowered
    # Запрет остался только на реально несуществующие функции
    forbidden_line = next(
        line for line in _ABILITIES_CONTEXT.splitlines() if "НИКОГДА не упоминай" in line
    )
    assert "игры" not in forbidden_line.lower()
    assert "монеты" not in forbidden_line.lower()


# ---------------------------------------------------------------------------
# Жалоба «Устарело» — персистентная запись + отчёт
# ---------------------------------------------------------------------------

def _run_stale_roundtrip(monkeypatch) -> tuple[list[str], str | None]:
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

        await unanswered.log_stale_report(100, "где аптека?", "Аптека на ул. Ленина 5")
        # Повтор той же жалобы — инкремент hits, не дубль
        await unanswered.log_stale_report(100, "где аптека?", "Аптека на ул. Ленина 5")
        reports = await unanswered.list_open_stale_reports()

        # Ответ админа закрывает жалобу; старый ответ бота в RAG не утекает
        async with session_factory() as session:
            from sqlalchemy import select
            from app.models import UnansweredQuestion
            q = (await session.execute(select(UnansweredQuestion))).scalars().one()
            question_id = q.id
        answered = await unanswered.save_admin_answer(question_id, "Аптека переехала на ул. Мира 3", 42)

        await engine.dispose()
        return reports, answered

    return asyncio.run(scenario())


def test_stale_report_roundtrip(monkeypatch) -> None:
    reports, answered = _run_stale_roundtrip(monkeypatch)
    assert len(reports) == 1
    assert "где аптека?" in reports[0]
    assert "×2" in reports[0]  # повтор посчитан, дубля нет
    assert answered is not None
    assert "где аптека?" in answered
