from datetime import datetime, timedelta, timezone

from app.handlers.help import (
    AI_CHAT_HISTORY,
    AI_CHAT_HISTORY_LIMIT,
    LAST_AI_REPLY_TIME,
    _get_ai_context,
    _is_ai_reply_rate_limited,
    _extract_ai_prompt,
    _remember_ai_exchange,
)


class _DummyMessage:
    def __init__(self, text: str | None = None, caption: str | None = None) -> None:
        self.text = text
        self.caption = caption
        self.entities = None
        self.caption_entities = None


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
