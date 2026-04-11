"""Почему: критичные Telegram-вызовы в модерации должны быть fail-safe и не ронять сценарий."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.handlers import moderation


@dataclass
class _Decision:
    severity: int
    violation_type: str | None = "aggression"
    confidence: float | None = 0.9
    sentiment: str | None = "negative"


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id

    def mention_html(self) -> str:
        return f"<a href='tg://user?id={self.id}'>user</a>"


class _Chat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _Message:
    def __init__(self, *, chat_id: int, user_id: int, message_id: int, text: str) -> None:
        self.chat = _Chat(chat_id)
        self.from_user = _User(user_id)
        self.message_id = message_id
        self.text = text
        self.message_thread_id = None
        self.deleted = False

    async def delete(self) -> bool:
        self.deleted = True
        return True


class _Session:
    async def commit(self) -> None:
        return None

    async def get(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def add(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        return None


async def _session_gen():
    yield _Session()


class _BotWithFailures:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.restrict_calls = 0
        self.raise_restrict = False
        self.raise_admin_send = False

    async def send_message(self, chat_id: int, text: str, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        if self.raise_admin_send and chat_id == 999_999:
            raise RuntimeError("admin log unavailable")
        self.sent_messages.append((chat_id, text))
        return True

    async def restrict_chat_member(self, *_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        self.restrict_calls += 1
        if self.raise_restrict:
            raise RuntimeError("restrict failed")
        return True

    async def ban_chat_member(self, *_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        raise RuntimeError("ban failed")


def test_apply_strike_threshold_does_not_crash_on_ban_error(monkeypatch) -> None:
    message = _Message(chat_id=1, user_id=7, message_id=55, text="x")
    bot = _BotWithFailures()

    monkeypatch.setattr(moderation, "get_session", _session_gen)

    asyncio.run(moderation._apply_strike_threshold(bot, message, user_id=7, strike_count=5))

    assert any("слишком много нарушений" in text for _, text in bot.sent_messages)


def test_check_flood_returns_true_when_restrict_fails(monkeypatch) -> None:
    message = _Message(chat_id=1, user_id=11, message_id=88, text="flood")
    bot = _BotWithFailures()
    bot.raise_restrict = True

    monkeypatch.setattr(moderation, "get_session", _session_gen)
    monkeypatch.setattr(
        moderation.FLOOD_TRACKER,
        "register",
        lambda *_args, **_kwargs: 11,
    )
    stored_events: list[tuple[int, int, str, int]] = []

    async def _store_event(chat_id: int, user_id: int, event_type: str, severity: int, **_kwargs) -> None:
        stored_events.append((chat_id, user_id, event_type, severity))

    monkeypatch.setattr(moderation, "_store_mod_event", _store_event)

    result = asyncio.run(moderation._check_flood(message, bot))

    assert result is True
    assert bot.restrict_calls == 1
    assert stored_events == [(1, 11, "mute", 2)]
    assert any("слишком частые сообщения" in text for _, text in bot.sent_messages)


def test_run_moderation_l3_degrades_when_delete_fails(monkeypatch) -> None:
    message = _Message(chat_id=1, user_id=42, message_id=101, text="очень грубый текст")

    async def _delete_fail() -> bool:
        raise RuntimeError("delete failed")

    message.delete = _delete_fail  # type: ignore[method-assign]

    bot = _BotWithFailures()
    bot.raise_admin_send = True

    monkeypatch.setattr("app.handlers.moderation.settings.forum_chat_id", 1, raising=False)
    monkeypatch.setattr("app.handlers.moderation.settings.admin_log_chat_id", 999_999, raising=False)
    monkeypatch.setattr("app.handlers.moderation.settings.ai_feature_moderation", False, raising=False)
    monkeypatch.setattr(moderation, "is_admin", lambda *_args, **_kwargs: asyncio.sleep(0, result=False))
    monkeypatch.setattr(moderation, "contains_forbidden_link", lambda _text: False)
    monkeypatch.setattr(moderation, "_get_topic_context", lambda *_args, **_kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(moderation, "_store_message_log", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(moderation, "_store_mod_event", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(moderation, "_apply_strike_threshold", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(moderation, "get_session", _session_gen)

    async def _add_strike(_session, _user_id: int, _chat_id: int) -> int:
        return 1

    monkeypatch.setattr(moderation, "add_strike", _add_strike)
    monkeypatch.setattr(
        "app.services.ai_module.local_moderation",
        lambda _text: _Decision(severity=3),
    )

    result = asyncio.run(moderation.run_moderation(message, bot))

    assert result is True
    assert bot.restrict_calls == 1
    assert any("не удалось удалить сообщение" in text for _, text in bot.sent_messages)
