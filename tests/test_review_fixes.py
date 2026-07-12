"""Регрессии по код-ревью: чистый вопрос в петле роста и фоновые задачи."""

from __future__ import annotations

import asyncio

from app.services.ai_module import (
    _BACKGROUND_TASKS,
    _spawn_background,
    _strip_prompt_scaffolding,
)


def test_strip_scaffolding_returns_bare_question() -> None:
    """Служебная преамбула контекста темы вырезается — остаётся вопрос жителя."""
    prompt = (
        "[Недавние сообщения в этой теме — контекст беседы]\n"
        "- сосед: опять шлагбаум не работает\n"
        "- другой сосед: да, стоит колом\n\n"
        "[Реплика, на которую отвечаешь]\n"
        "Куда звонить если шлагбаум сломался?"
    )
    assert _strip_prompt_scaffolding(prompt) == "Куда звонить если шлагбаум сломался?"


def test_strip_scaffolding_removes_dialog_prefix_and_trailing_note() -> None:
    prompt = (
        "[Реплика, на которую отвечаешь]\n"
        "[Продолжение диалога — предыдущий ответ бота: привет]\n\n"
        "А где ближайшая почта?\n"
        "[Продолжительный диалог — после ответа предложи итог]"
    )
    assert _strip_prompt_scaffolding(prompt) == "А где ближайшая почта?"


def test_strip_scaffolding_passthrough_plain_question() -> None:
    """Без преамбулы вопрос не меняется."""
    assert _strip_prompt_scaffolding("Где аптека?") == "Где аптека?"


def test_spawn_background_keeps_reference_until_done() -> None:
    """Фоновая задача держится в наборе, пока не завершится (иначе GC съест)."""

    async def _run() -> bool:
        done = asyncio.Event()

        async def _job() -> None:
            done.set()

        _spawn_background(_job())
        assert _BACKGROUND_TASKS, "ссылка на задачу должна удерживаться"
        await done.wait()
        await asyncio.sleep(0)  # даём done_callback снять ссылку
        return True

    assert asyncio.run(_run())
