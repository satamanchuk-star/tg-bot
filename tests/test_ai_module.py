import asyncio

import httpx
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
    _extract_response_content,
    _normalize_model_id,
    get_ai_client,
    is_ai_runtime_enabled,
    set_ai_runtime_enabled,
    resolve_provider_mode,
    reload_profanity_runtime,
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


def test_normalize_model_id_replaces_decimal_commas() -> None:
    assert _normalize_model_id("qwen/qwen3,5-flash") == "qwen/qwen3.5-flash"
    assert _normalize_model_id("qwen/qwen3，5-flash") == "qwen/qwen3.5-flash"


def test_extract_response_content_supports_string() -> None:
    payload = {"choices": [{"message": {"content": "Привет"}}]}
    assert _extract_response_content(payload) == "Привет"


def test_extract_response_content_supports_content_parts() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "Первая часть"},
                        {"type": "text", "text": "Вторая часть"},
                    ]
                }
            }
        ]
    }
    assert _extract_response_content(payload) == "Первая часть\nВторая часть"

def test_detects_masked_profanity() -> None:
    normalized = normalize_for_profanity("Да ты б*л_я!")
    assert detect_profanity(normalized)


def test_reload_profanity_runtime_changes_detect_behavior(monkeypatch) -> None:
    from app.services import ai_module

    original_loader = ai_module.reload_profanity_runtime_dict
    monkeypatch.setattr(
        "app.services.ai_module.reload_profanity_runtime_dict",
        lambda: {"exact": {"грубость"}, "prefixes": set(), "exceptions": set()},
    )
    reload_profanity_runtime()
    assert detect_profanity(normalize_for_profanity("Это грубость"))

    monkeypatch.setattr(
        "app.services.ai_module.reload_profanity_runtime_dict",
        lambda: {"exact": set(), "prefixes": set(), "exceptions": set()},
    )
    reload_profanity_runtime()
    assert not detect_profanity(normalize_for_profanity("Это грубость"))

    monkeypatch.setattr("app.services.ai_module.reload_profanity_runtime_dict", original_loader)
    reload_profanity_runtime()

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
    assert "локальном режиме" not in reply.lower()
    assert len(reply.strip()) > 0


def test_local_assistant_reply_handles_rules_and_mentions() -> None:
    reply = build_local_assistant_reply("@jabchat_bot какие правила по шуму ночью?")
    assert "правил" in reply.lower() or "уваж" in reply.lower()




def test_local_assistant_reply_unknown_question_is_friendly() -> None:
    reply = build_local_assistant_reply("Где телепорт на Марс в нашем ЖК?")
    assert len(reply.strip()) > 20


def test_local_assistant_reply_uses_places_hint(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.ai_module.build_resident_answer",
        lambda prompt, *, context=None: None,  # type: ignore[return-value]
    )

    reply = build_local_assistant_reply(
        "Где ближайшее МФЦ?",
        places_hint="- МФЦ Видное (Госучреждения), адрес: ул. Центральная, 1",
    )
    assert "мфц видное" in reply.lower()


def test_local_assistant_reply_prioritizes_resident_kb_over_places(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.ai_module.build_resident_answer",
        lambda prompt, *, context=None: "Ответ по шлагбауму из базы ЖК",  # type: ignore[return-value]
    )

    reply = build_local_assistant_reply(
        "Как оформить пропуск на шлагбаум?",
        places_hint="- Школа №1, адрес: ул. Центральная, 10",
    )

    assert "шлагбаум" in reply.lower()
    assert "школа" not in reply.lower()


def test_local_assistant_reply_prioritizes_rag_over_places(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.ai_module.build_resident_answer",
        lambda prompt, *, context=None: None,  # type: ignore[return-value]
    )

    reply = build_local_assistant_reply(
        "Как оформить пропуск?",
        rag_hint="[1] (доступ) Гостевой пропуск оформляется через форму УК.",
        places_hint="- МФЦ Видное, адрес: ул. Центральная, 1",
    )

    assert "гостевой пропуск" in reply.lower()
    assert "мфц" not in reply.lower()




def test_detects_masked_profanity_with_latin_and_digits() -> None:
    normalized = normalize_for_profanity("Ты п1зд@бол")
    assert detect_profanity(normalized)


def test_aggression_level_and_warning_action() -> None:
    assert detect_aggression_level("Ты бля не прав") == "low"
    decision = local_moderation("Ты бля не прав")
    assert decision.action == "none"
    assert decision.severity == 0


def test_high_aggression_keeps_strict_action() -> None:
    decision = local_moderation("Я тебя убью")
    assert decision.action == "delete_strike"
    assert decision.severity == 3

def test_assistant_prompt_has_human_style_and_limits() -> None:
    assert "Ты — бот-помощник" in _ASSISTANT_SYSTEM_PROMPT
    assert "тот самый сосед" in _ASSISTANT_SYSTEM_PROMPT
    assert "до 800 символов" in _ASSISTANT_SYSTEM_PROMPT
    assert "с отличным чувством юмора" in _ASSISTANT_SYSTEM_PROMPT
    assert "Если в контексте нет точной информации" in _ASSISTANT_SYSTEM_PROMPT
    assert "Приоритет источников" in _ASSISTANT_SYSTEM_PROMPT


def test_moderation_prompt_has_basic_safety_limits() -> None:
    assert "Верни только JSON" in _MODERATION_SYSTEM_PROMPT
    assert "При сомнении между severity 0 и 1 — ставь 0" in _MODERATION_SYSTEM_PROMPT


def test_openrouter_assistant_fallback_on_runtime_error(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_chat_completion", _raise)
    reply = asyncio.run(provider.assistant_reply("вопрос про шлагбаум", [], chat_id=1))
    assert "шлагбаум" in reply.lower()
    asyncio.run(provider.aclose())


def test_openrouter_assistant_prefers_resident_kb_before_remote(monkeypatch) -> None:
    provider = OpenRouterProvider()

    monkeypatch.setattr(
        "app.services.ai_module.build_resident_answer",
        lambda prompt, *, context=None: "Точный ответ из канонической базы",  # type: ignore[return-value]
    )

    async def _raise_if_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("remote completion should not be called")

    monkeypatch.setattr(provider, "_chat_completion", _raise_if_called)

    reply = asyncio.run(provider.assistant_reply("Какие в ЖК есть магазины?", [], chat_id=1))
    assert reply == "Точный ответ из канонической базы"
    asyncio.run(provider.aclose())




def test_openrouter_assistant_fallback_on_http_400(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _bad_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad request"}})
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(provider._client, "post", _bad_request)
    reply = asyncio.run(provider.assistant_reply("вопрос про шлагбаум", [], chat_id=1))
    assert "шлагбаум" in reply.lower()
    asyncio.run(provider.aclose())


def test_openrouter_retries_with_fallback_model_on_invalid_id(monkeypatch) -> None:
    provider = OpenRouterProvider()
    sent_models: list[str] = []

    async def _fake_add_usage(chat_id: int, tokens: int) -> None:
        return None

    async def _post(*args, **kwargs):  # type: ignore[no-untyped-def]
        model = kwargs["json"]["model"]
        sent_models.append(model)
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        if len(sent_models) == 1:
            return httpx.Response(
                400,
                request=request,
                json={"error": {"message": f"{model} is not a valid model ID"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 12},
            },
        )

    async def _allow(chat_id: int) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)
    monkeypatch.setattr("app.services.ai_module._can_use_remote_ai", _allow)
    monkeypatch.setattr("app.services.ai_module._add_remote_usage", _fake_add_usage)
    monkeypatch.setattr(provider._client, "post", _post)

    content, tokens = asyncio.run(provider._chat_completion([{"role": "user", "content": "ping"}], chat_id=1))

    assert content == "ok"
    assert tokens == 12
    assert len(sent_models) == 2
    assert sent_models[-1] == "openrouter/auto"
    asyncio.run(provider.aclose())


def test_openrouter_retries_with_fallback_model_on_no_endpoints(monkeypatch) -> None:
    provider = OpenRouterProvider()
    sent_models: list[str] = []

    async def _fake_add_usage(chat_id: int, tokens: int) -> None:
        return None

    async def _post(*args, **kwargs):  # type: ignore[no-untyped-def]
        model = kwargs["json"]["model"]
        sent_models.append(model)
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        if len(sent_models) == 1:
            return httpx.Response(
                404,
                request=request,
                json={"error": {"message": f"No endpoints found for {model}"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 7},
            },
        )

    async def _allow(chat_id: int) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)
    monkeypatch.setattr("app.services.ai_module._can_use_remote_ai", _allow)
    monkeypatch.setattr("app.services.ai_module._add_remote_usage", _fake_add_usage)
    monkeypatch.setattr(provider._client, "post", _post)

    content, tokens = asyncio.run(provider._chat_completion([{"role": "user", "content": "ping"}], chat_id=1))

    assert content == "ok"
    assert tokens == 7
    assert len(sent_models) == 2
    assert sent_models[-1] == "openrouter/auto"
    asyncio.run(provider.aclose())

def test_openrouter_summary_fallback_on_runtime_error(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_chat_completion", _raise)
    result = asyncio.run(provider.generate_daily_summary("ctx", chat_id=1))
    assert result is None
    asyncio.run(provider.aclose())


def test_openrouter_chat_completion_raises_on_empty_content(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _fake_add_usage(chat_id: int, tokens: int) -> None:
        return None

    async def _post(*args, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": None}}],
                "usage": {"total_tokens": 0},
            },
        )

    async def _allow(chat_id: int) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)
    monkeypatch.setattr("app.services.ai_module._can_use_remote_ai", _allow)
    monkeypatch.setattr("app.services.ai_module._add_remote_usage", _fake_add_usage)
    monkeypatch.setattr(provider._client, "post", _post)

    try:
        asyncio.run(provider._chat_completion([{"role": "user", "content": "ping"}], chat_id=1))
    except RuntimeError as exc:
        assert "пустой текст" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for empty AI content")
    finally:
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

    assert "локальном режиме" not in reply.lower()
    assert len(reply.strip()) > 0


def test_extract_search_words_adds_stem_variant_for_school_words() -> None:
    words = _extract_search_words("Какая школа рядом?")
    assert "школа" in words
    assert "школ" in words


def test_runtime_flag_is_enabled_by_default() -> None:
    assert is_ai_runtime_enabled() is True


def test_resolve_provider_mode_respects_runtime_flag(monkeypatch) -> None:
    monkeypatch.setattr("app.services.ai_module.settings.ai_enabled", True, raising=False)
    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)

    set_ai_runtime_enabled(False)
    assert resolve_provider_mode() == "stub"

    set_ai_runtime_enabled(True)
    assert resolve_provider_mode() == "remote"


def test_get_ai_client_uses_stub_when_runtime_disabled(monkeypatch) -> None:
    monkeypatch.setattr("app.services.ai_module._AI_CLIENT", None, raising=False)
    monkeypatch.setattr("app.services.ai_module.settings.ai_enabled", True, raising=False)
    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)

    set_ai_runtime_enabled(False)
    client = get_ai_client()

    assert type(client._provider).__name__ == "StubAiProvider"


def test_runtime_toggle_recreates_client(monkeypatch) -> None:
    monkeypatch.setattr("app.services.ai_module._AI_CLIENT", None, raising=False)
    monkeypatch.setattr("app.services.ai_module.settings.ai_enabled", True, raising=False)
    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)

    set_ai_runtime_enabled(False)
    first = get_ai_client()
    set_ai_runtime_enabled(True)
    second = get_ai_client()

    assert first is not second

    asyncio.run(second.aclose())
