from app.handlers.help import (
    AI_CHAT_HISTORY,
    AI_CHAT_HISTORY_LIMIT,
    _get_ai_context,
    _remember_ai_exchange,
)


def setup_function() -> None:
    AI_CHAT_HISTORY.clear()


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
