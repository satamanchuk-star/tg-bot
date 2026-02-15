import asyncio

from app.services.ai_module import (
    AiModuleClient,
    _ASSISTANT_SYSTEM_PROMPT,
    _MODERATION_SYSTEM_PROMPT,
    OpenRouterProvider,
    build_local_assistant_reply,
    detect_profanity,
    get_ai_diagnostics,
    is_assistant_topic_allowed,
    local_quiz_answer_decision,
    mask_personal_data,
    normalize_for_profanity,
    parse_quiz_answer_response,
)


def test_detects_masked_profanity() -> None:
    normalized = normalize_for_profanity("Да ты б*л_я!")
    assert detect_profanity(normalized)


def test_masks_personal_data() -> None:
    masked = mask_personal_data("Иван Иванов, +79991234567, test@example.com")
    assert "+79991234567" not in masked
    assert "test@example.com" not in masked


def test_assistant_topic_restrictions() -> None:
    assert is_assistant_topic_allowed("Как решить проблему со шлагбаумом?")
    assert is_assistant_topic_allowed("Какие правила по шуму в ЖК?")
    assert not is_assistant_topic_allowed("Дай финансовый совет")


def test_parse_quiz_answer_response() -> None:
    decision = parse_quiz_answer_response({"is_correct": True, "confidence": 0.9})
    assert decision.is_correct is True
    assert decision.is_close is True


def test_local_quiz_answer_close_match() -> None:
    decision = local_quiz_answer_decision("домофон в подъезде", "домофон")
    assert decision.is_close is True


def test_probe_returns_stub_status() -> None:
    result = asyncio.run(AiModuleClient().probe())
    assert result.ok is False
    assert "stub" in result.details.lower()


def test_assistant_reply_uses_local_fallback() -> None:
    reply = asyncio.run(AiModuleClient().assistant_reply("вопрос про шлагбаум", [], chat_id=1))
    assert "Модуль ИИ" in reply


def test_local_assistant_reply_handles_rules_and_mentions() -> None:
    reply = build_local_assistant_reply("@jabchat_bot какие правила по шуму ночью?")
    assert "шум" in reply.lower()
    assert "фактов" in reply.lower()


def test_assistant_prompt_has_human_style_and_limits() -> None:
    assert "как живой человек" in _ASSISTANT_SYSTEM_PROMPT
    assert "без упоминания, что ты ИИ" in _ASSISTANT_SYSTEM_PROMPT
    assert "до 800 символов" in _ASSISTANT_SYSTEM_PROMPT


def test_moderation_prompt_has_basic_safety_limits() -> None:
    assert "Верни только JSON" in _MODERATION_SYSTEM_PROMPT
    assert "при сомнении выбирай более мягкое действие" in _MODERATION_SYSTEM_PROMPT


def test_openrouter_assistant_fallback_on_runtime_error(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_chat_completion", _raise)
    reply = asyncio.run(provider.assistant_reply("вопрос про шлагбаум", [], chat_id=1))
    assert "шлагбаум" in reply.lower()
    asyncio.run(provider.aclose())


def test_openrouter_summary_fallback_on_runtime_error(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_chat_completion", _raise)
    result = asyncio.run(provider.generate_daily_summary("ctx", chat_id=1))
    assert result is None
    asyncio.run(provider.aclose())


def test_get_ai_diagnostics_for_stub(monkeypatch) -> None:
    async def _fake_usage(chat_id: int) -> tuple[int, int]:
        return (0, 0)

    monkeypatch.setattr("app.services.ai_module.get_ai_usage_for_today", _fake_usage)
    report = asyncio.run(get_ai_diagnostics(chat_id=1))
    assert report.provider_mode == "stub"
    assert report.probe_ok is False
