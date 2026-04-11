"""Почему: защищаемся от повторной модерации одного и того же message_id."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import asyncio

from app.handlers import moderation


@pytest.fixture(autouse=True)
def clear_moderated_cache() -> None:
    """Очищаем in-memory кеш dedup перед/после теста."""
    moderation._MODERATED_MSG_IDS.clear()
    yield
    moderation._MODERATED_MSG_IDS.clear()


def _build_message(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=12345),
        from_user=SimpleNamespace(id=777, mention_html=lambda: "@u"),
        text="обычное сообщение",
        message_id=message_id,
        message_thread_id=99,
        delete=AsyncMock(),
    )


def test_second_call_with_same_message_id_is_skipped(monkeypatch) -> None:
    """Повторная модерация того же message_id не должна доходить до AI."""
    monkeypatch.setattr(moderation.settings, "forum_chat_id", 12345)
    monkeypatch.setattr(moderation, "is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(moderation, "contains_forbidden_link", lambda _: False)
    monkeypatch.setattr(moderation, "_get_topic_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(moderation, "_store_message_log", AsyncMock())
    monkeypatch.setattr(moderation, "_check_flood", AsyncMock(return_value=False))

    ai_moderate = AsyncMock(return_value=SimpleNamespace(severity=0, sentiment="neutral"))
    monkeypatch.setattr(moderation, "get_ai_client", lambda: SimpleNamespace(moderate=ai_moderate))
    monkeypatch.setattr(moderation.settings, "ai_feature_moderation", True)

    bot = AsyncMock()
    message = _build_message(message_id=1001)

    first = asyncio.run(moderation.run_moderation(message, bot))
    second = asyncio.run(moderation.run_moderation(message, bot))

    assert first is False
    assert second is False
    assert ai_moderate.await_count == 1


def test_dedup_cache_is_trimmed_when_overflow(monkeypatch) -> None:
    """При переполнении кеш сокращается, чтобы не расти бесконечно."""
    monkeypatch.setattr(moderation.settings, "forum_chat_id", 12345)
    monkeypatch.setattr(moderation, "is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(moderation, "contains_forbidden_link", lambda _: False)
    monkeypatch.setattr(moderation, "_get_topic_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(moderation, "_store_message_log", AsyncMock())
    monkeypatch.setattr(moderation, "_check_flood", AsyncMock(return_value=False))

    ai_moderate = AsyncMock(return_value=SimpleNamespace(severity=0, sentiment="neutral"))
    monkeypatch.setattr(moderation, "get_ai_client", lambda: SimpleNamespace(moderate=ai_moderate))
    monkeypatch.setattr(moderation.settings, "ai_feature_moderation", True)

    moderation._MODERATED_MSG_IDS.update(range(1, moderation._MODERATED_MSG_IDS_MAX + 2))
    before = len(moderation._MODERATED_MSG_IDS)

    asyncio.run(moderation.run_moderation(_build_message(message_id=999999), AsyncMock()))

    assert before > moderation._MODERATED_MSG_IDS_MAX
    assert len(moderation._MODERATED_MSG_IDS) <= moderation._MODERATED_MSG_IDS_MAX // 2 + 2
