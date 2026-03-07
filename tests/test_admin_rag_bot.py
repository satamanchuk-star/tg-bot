import asyncio
from dataclasses import dataclass

from app.handlers.admin import rag_bot_command


@dataclass
class _DummyCategorizeResult:
    category: str = "общее"
    summary: str = "summary"
    used_fallback: bool = True


class _DummyAiClient:
    async def categorize_rag_entry(self, text: str, *, chat_id: int) -> _DummyCategorizeResult:
        return _DummyCategorizeResult()


class _DummyUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _DummyReplyMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.caption = None
        self.message_id = 77
        self.from_user = _DummyUser(200)


class _DummyMessage:
    def __init__(self) -> None:
        self.from_user = _DummyUser(100)
        self.reply_to_message = _DummyReplyMessage("Очень важное новое знание по ЖК.")
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class _DummyBot:
    pass


class _DummySession:
    async def commit(self) -> None:
        return None


async def _fake_session_gen():
    yield _DummySession()


def test_rag_bot_canonicalizes_without_admin_priority(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _allow_admin(_message, _bot) -> bool:
        return True

    async def _fake_add_rag_message(session, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)

        class _Record:
            rag_category = "общее"
            rag_canonical_text = kwargs["message_text"]

        return _Record()

    async def _fake_systematize(_session, _chat_id: int) -> int:
        return 0

    async def _fake_count(_session, _chat_id: int) -> int:
        return 1

    monkeypatch.setattr("app.handlers.admin._ensure_admin", _allow_admin)
    monkeypatch.setattr("app.handlers.admin.get_ai_client", lambda: _DummyAiClient())
    monkeypatch.setattr("app.handlers.admin.get_session", lambda: _fake_session_gen())
    monkeypatch.setattr("app.handlers.admin.add_rag_message", _fake_add_rag_message)
    monkeypatch.setattr("app.handlers.admin.systematize_rag", _fake_systematize)
    monkeypatch.setattr("app.handlers.admin.get_rag_count", _fake_count)

    message = _DummyMessage()
    asyncio.run(rag_bot_command(message, _DummyBot()))

    assert "is_admin" not in captured
    assert captured["message_text"] == "Очень важное новое знание по ЖК."
    assert message.replies
