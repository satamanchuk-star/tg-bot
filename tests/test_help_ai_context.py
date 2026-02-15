from datetime import datetime, timedelta, timezone

import asyncio

from app.config import settings
from app.handlers.help import (
    AI_CHAT_HISTORY,
    AI_CHAT_HISTORY_LIMIT,
    AI_RATE_LIMIT_TEXT,
    LAST_AI_REPLY_TIME,
    _extract_ai_prompt,
    _get_ai_context,
    _is_ai_reply_rate_limited,
    _remember_ai_exchange,
    ai_command,
    mention_help,
)


class _DummyMessage:
    def __init__(self, text: str | None = None, caption: str | None = None) -> None:
        self.text = text
        self.caption = caption
        self.entities = None
        self.caption_entities = None


class _DummyChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _DummyUser:
    def __init__(self, user_id: int, *, is_bot: bool = False) -> None:
        self.id = user_id
        self.is_bot = is_bot


class _DummyIncomingMessage:
    def __init__(self, chat_id: int, user_id: int, text: str) -> None:
        self.chat = _DummyChat(chat_id)
        self.from_user = _DummyUser(user_id)
        self.text = text
        self.caption = None
        self.entities = None
        self.caption_entities = None
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class _DummyBotIdentity:
    username = None


class _DummyBot:
    async def get_me(self) -> _DummyBotIdentity:
        return _DummyBotIdentity()


def setup_function() -> None:
    AI_CHAT_HISTORY.clear()
    LAST_AI_REPLY_TIME.clear()


def test_ai_context_remembers_previous_messages() -> None:
    _remember_ai_exchange(1, 2, "Первый вопрос", "Первый ответ")
    _remember_ai_exchange(1, 2, "Второй вопрос", "Второй ответ")

    context = _get_ai_context(1, 2)

    assert context == [
        "user: Первый вопрос",
        "assistant: Первый ответ",
        "user: Второй вопрос",
        "assistant: Второй ответ",
    ]


def test_ai_context_is_limited() -> None:
    for index in range(AI_CHAT_HISTORY_LIMIT):
        _remember_ai_exchange(10, 20, f"q{index}", f"a{index}")

    context = _get_ai_context(10, 20)

    assert len(context) == AI_CHAT_HISTORY_LIMIT
    assert context[0] == "user: q10"
    assert context[-1] == "assistant: a19"


def test_extract_ai_prompt_from_command() -> None:
    message = _DummyMessage(text="/ai Как открыть шлагбаум?")
    assert _extract_ai_prompt(message) == "Как открыть шлагбаум?"


def test_extract_ai_prompt_from_plain_text() -> None:
    message = _DummyMessage(text="@alexbot помоги с подъездом")
    assert _extract_ai_prompt(message) == "@alexbot помоги с подъездом"


def test_ai_reply_rate_limit_blocks_fast_repeat() -> None:
    assert _is_ai_reply_rate_limited(1, 2) is False
    assert _is_ai_reply_rate_limited(1, 2) is True

    LAST_AI_REPLY_TIME[(1, 2)] = datetime.now(timezone.utc) - timedelta(seconds=21)
    assert _is_ai_reply_rate_limited(1, 2) is False


def test_ai_command_returns_hint_outside_forum_chat() -> None:
    message = _DummyIncomingMessage(
        chat_id=settings.forum_chat_id + 1,
        user_id=100,
        text="/ai test",
    )

    asyncio.run(ai_command(message))

    assert message.replies == ["Команда /ai работает только в форуме ЖК."]


def test_mention_help_returns_rate_limit_message() -> None:
    message = _DummyIncomingMessage(
        chat_id=settings.forum_chat_id,
        user_id=100,
        text="@bot помоги",
    )
    LAST_AI_REPLY_TIME[(settings.forum_chat_id, 100)] = datetime.now(timezone.utc)

    asyncio.run(mention_help(message, _DummyBot()))

    assert message.replies == [AI_RATE_LIMIT_TEXT]
