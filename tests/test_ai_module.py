import asyncio
from app.services.ai_module import (
    AiModuleClient,
    _ASSISTANT_SYSTEM_PROMPT,
    _MODERATION_SYSTEM_PROMPT,
    OpenRouterProvider,
    build_local_assistant_reply,
    detect_aggression_level,
    detect_profanity,
    local_moderation,
    get_ai_diagnostics,
    is_assistant_topic_allowed,
    local_quiz_answer_decision,
    mask_personal_data,
    normalize_for_profanity,
    parse_quiz_answer_response,
    _extract_search_words,
)


class _SlowProvider:
    async def probe(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)

    async def moderate(self, text: str, *, chat_id: int, context: list[str] | None = None):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        await asyncio.sleep(0.1)
        return "remote"

    async def evaluate_quiz_answer(  # type: ignore[no-untyped-def]
        self,
        question: str,
        correct_answer: str,
        user_answer: str,
        *,
        chat_id: int,
    ):
        await asyncio.sleep(0.1)

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str:
        await asyncio.sleep(0.1)
        return "summary"

    async def categorize_rag_entry(self, text: str, *, chat_id: int):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)


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
    assert "правил" in reply.lower() or "уваж" in reply.lower()




def test_local_assistant_reply_unknown_question_is_friendly() -> None:
    reply = build_local_assistant_reply("Где телепорт на Марс в нашем ЖК?")
    assert len(reply.strip()) > 20


def test_local_assistant_reply_uses_places_hint() -> None:
    reply = build_local_assistant_reply(
        "Где ближайшее МФЦ?",
        places_hint="- МФЦ Видное (Госучреждения), адрес: ул. Центральная, 1",
    )
    assert "базе инфраструктуры" in reply.lower()
    assert "мфц видное" in reply.lower()




def test_detects_masked_profanity_with_latin_and_digits() -> None:
    normalized = normalize_for_profanity("Ты п1зд@бол")
    assert detect_profanity(normalized)


def test_aggression_level_and_warning_action() -> None:
    assert detect_aggression_level("Ты бля не прав") == "low"
    decision = local_moderation("Ты бля не прав")
    assert decision.action == "warn"
    assert decision.severity == 1


def test_high_aggression_keeps_strict_action() -> None:
    decision = local_moderation("Я тебя убью")
    assert decision.action == "delete_strike"
    assert decision.severity == 3

def test_assistant_prompt_has_human_style_and_limits() -> None:
    assert "дружелюбный сосед-помощник" in _ASSISTANT_SYSTEM_PROMPT
    assert "как живой человек" in _ASSISTANT_SYSTEM_PROMPT
    assert "без упоминания, что ты ИИ" in _ASSISTANT_SYSTEM_PROMPT
    assert "до 800 символов" in _ASSISTANT_SYSTEM_PROMPT
    assert "детскую площадку" in _ASSISTANT_SYSTEM_PROMPT
    assert "платежи" in _ASSISTANT_SYSTEM_PROMPT
    assert "дружелюбная атмосфера" in _ASSISTANT_SYSTEM_PROMPT
    assert "точной информации нет" in _ASSISTANT_SYSTEM_PROMPT


def test_moderation_prompt_has_basic_safety_limits() -> None:
    assert "Верни только JSON" in _MODERATION_SYSTEM_PROMPT
    assert "При ЛЮБОМ сомнении" in _MODERATION_SYSTEM_PROMPT


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


def test_ai_module_client_moderation_timeout_fallback(monkeypatch) -> None:
    monkeypatch.setattr("app.services.ai_module._MODERATION_SOFT_TIMEOUT_SECONDS", 0.01)
    client = AiModuleClient(provider=_SlowProvider())

    decision = asyncio.run(client.moderate("привет", chat_id=1))

    assert decision.used_fallback is True
    assert decision.action == "none"


def test_ai_module_client_assistant_timeout_fallback(monkeypatch) -> None:
    monkeypatch.setattr("app.services.ai_module._ASSISTANT_SOFT_TIMEOUT_SECONDS", 0.01)
    client = AiModuleClient(provider=_SlowProvider())

    reply = asyncio.run(client.assistant_reply("шлагбаум не работает", [], chat_id=1))

    assert "Модуль ИИ" in reply


def test_extract_search_words_adds_stem_variant_for_school_words() -> None:
    words = _extract_search_words("Какая школа рядом?")
    assert "школа" in words
    assert "школ" in words
