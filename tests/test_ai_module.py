import asyncio

import httpx
from app.services.ai_module import (
    AiModuleClient,
    _ASSISTANT_SYSTEM_PROMPT,
    _MODERATION_SYSTEM_PROMPT,
    AnthropicProvider,
    build_local_assistant_reply,
    detect_aggression_level,
    detect_profanity,
    local_moderation,
    get_ai_diagnostics,
    is_assistant_topic_allowed,
    mask_personal_data,
    normalize_for_profanity,
    _extract_search_words,
    _parse_context_line,
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

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int, user_id: int | None = None, topic_id: int | None = None) -> str:
        await asyncio.sleep(0.1)
        return "remote"

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str:
        await asyncio.sleep(0.1)
        return "summary"

    async def categorize_rag_entry(self, text: str, *, chat_id: int):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)


def test_normalize_model_id_replaces_decimal_commas() -> None:
    assert _normalize_model_id("qwen/qwen3,5-flash") == "qwen/qwen3.5-flash"
    assert _normalize_model_id("qwen/qwen3，5-flash") == "qwen/qwen3.5-flash"



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
    assert len(reply.strip()) > 0
    assert len(reply.strip()) <= 90


def test_local_assistant_reply_uses_places_hint(monkeypatch) -> None:
    from app.services.resident_kb import ResidentKbSearchResult

    monkeypatch.setattr(
        "app.services.ai_module.search_resident_kb",
        lambda prompt, *, context=None, top_k=1: ResidentKbSearchResult(matches=[], exact=False),
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
    assert "с живым чувством юмора" in _ASSISTANT_SYSTEM_PROMPT
    assert "400 символов" in _ASSISTANT_SYSTEM_PROMPT
    assert "нет точной информации" in _ASSISTANT_SYSTEM_PROMPT
    assert "Приоритет источников" in _ASSISTANT_SYSTEM_PROMPT


def test_moderation_prompt_has_basic_safety_limits() -> None:
    assert "Верни только JSON" in _MODERATION_SYSTEM_PROMPT
    assert "ПРЕЗУМПЦИЯ НЕВИНОВНОСТИ" in _MODERATION_SYSTEM_PROMPT
    assert "При ЛЮБОМ сомнении понижай severity" in _MODERATION_SYSTEM_PROMPT


def test_openrouter_assistant_fallback_on_runtime_error(monkeypatch) -> None:
    provider = AnthropicProvider()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_chat_completion", _raise)
    reply = asyncio.run(provider.assistant_reply("вопрос про шлагбаум", [], chat_id=1))
    assert "шлагбаум" in reply.lower()
    asyncio.run(provider.aclose())


def test_assistant_stays_silent_when_ungrounded(monkeypatch) -> None:
    """«Реже, но точнее»: фактический вопрос без опоры → честный не-знаю, без вызова модели."""
    from app.services import ai_module

    provider = AnthropicProvider()

    async def _empty(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ""

    # Ни один источник знаний не сматчился.
    monkeypatch.setattr(ai_module, "build_resident_context", lambda *a, **k: "")
    monkeypatch.setattr(ai_module, "should_search_web", lambda *a, **k: False)
    monkeypatch.setattr(provider, "_get_rag_context", _empty, raising=False)
    monkeypatch.setattr(ai_module, "_get_rag_context", _empty)
    monkeypatch.setattr(ai_module, "_get_faq_answer", _empty)
    monkeypatch.setattr(ai_module, "_get_places_context", _empty)

    # Модель не должна вызываться — гейт срабатывает раньше.
    async def _must_not_call(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("_chat_completion не должен вызываться при отсутствии опоры")

    monkeypatch.setattr(provider, "_chat_completion", _must_not_call)

    reply = asyncio.run(provider.assistant_reply(
        "какой тариф на отопление в нашем доме в этом месяце", [], chat_id=1,
    ))
    assert reply in ai_module._UNGROUNDED_REPLIES
    asyncio.run(provider.aclose())


def _patch_empty_knowledge(monkeypatch, provider) -> None:
    """Все источники знаний пустые — для тестов гейта."""
    from app.services import ai_module

    async def _empty(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ""

    monkeypatch.setattr(ai_module, "build_resident_context", lambda *a, **k: "")
    monkeypatch.setattr(ai_module, "should_search_web", lambda *a, **k: False)
    monkeypatch.setattr(ai_module, "_get_rag_context", _empty)
    monkeypatch.setattr(ai_module, "_get_faq_answer", _empty)
    monkeypatch.setattr(ai_module, "_get_places_context", _empty)


def test_gate_lets_drafting_requests_through(monkeypatch) -> None:
    """Творческая просьба без опоры в KB НЕ гейтится — уходит в модель."""
    provider = AnthropicProvider()
    _patch_empty_knowledge(monkeypatch, provider)

    called: list[bool] = []

    async def _fake_completion(messages, *, chat_id, **kwargs):  # type: ignore[no-untyped-def]
        called.append(True)
        return ("Объявление: субботник в воскресенье в 10:00.", 10)

    monkeypatch.setattr(provider, "_chat_completion", _fake_completion)

    reply = asyncio.run(provider.assistant_reply(
        "напиши объявление о субботнике в воскресенье", [], chat_id=1,
    ))
    assert called, "творческая просьба должна дойти до модели"
    assert "субботник" in reply.lower()
    asyncio.run(provider.aclose())


def test_gate_lets_short_followup_through(monkeypatch) -> None:
    """Короткий follow-up в живом диалоге НЕ гейтится: ответ может быть в контексте."""
    provider = AnthropicProvider()
    _patch_empty_knowledge(monkeypatch, provider)

    called: list[bool] = []

    async def _fake_completion(messages, *, chat_id, **kwargs):  # type: ignore[no-untyped-def]
        called.append(True)
        return ("В 10 утра, как договаривались.", 5)

    monkeypatch.setattr(provider, "_chat_completion", _fake_completion)

    context = [
        "user: когда собираемся на субботник?",
        "assistant: В воскресенье в 10:00 у второго подъезда.",
    ]
    reply = asyncio.run(provider.assistant_reply("а во сколько?", context, chat_id=1))
    assert called, "короткий follow-up должен дойти до модели"
    assert reply
    asyncio.run(provider.aclose())


def test_openrouter_assistant_includes_resident_kb_in_context(monkeypatch) -> None:
    """KB-контент передаётся как контекст в AI (не bypasses AI)."""
    provider = AnthropicProvider()

    kb_text = "Точный ответ из канонической базы"
    monkeypatch.setattr(
        "app.services.ai_module.build_resident_context",
        lambda prompt, *, context=None: kb_text,
    )

    captured: list[list[dict]] = []

    async def _fake_completion(messages: list[dict], *, chat_id: int, **kwargs) -> tuple[str, int]:
        captured.append(messages)
        return ("ai answer", 10)

    monkeypatch.setattr(provider, "_chat_completion", _fake_completion)

    async def _run() -> None:
        await provider.assistant_reply("Какие в ЖК есть магазины?", [], chat_id=1)
        await provider.aclose()

    asyncio.run(_run())

    assert len(captured) == 1
    system_text = " ".join(
        str(m.get("content", "")) for m in captured[0] if m.get("role") == "system"
    )
    assert kb_text in system_text


def test_openrouter_assistant_includes_history_summary_context(monkeypatch) -> None:
    provider = AnthropicProvider()
    summary = "Краткий контекст диалога:\n- Вы: ранее обсуждали шлагбаум"
    captured: list[list[dict]] = []

    async def _fake_completion(messages: list[dict], *, chat_id: int, **kwargs) -> tuple[str, int]:
        captured.append(messages)
        return ("ai answer", 5)

    monkeypatch.setattr(provider, "_chat_completion", _fake_completion)

    async def _run() -> None:
        await provider.assistant_reply("и что дальше делать?", [summary], chat_id=1)
        await provider.aclose()

    asyncio.run(_run())

    assert captured
    roles = [m.get("role") for m in captured[0]]
    assert "system" in roles
    assert any(
        m.get("role") == "system" and summary in str(m.get("content", ""))
        for m in captured[0]
    )


def test_parse_context_line_supports_bracket_format() -> None:
    role, text = _parse_context_line("[user_101]: А что с лифтом?")
    assert role == "user"
    assert text == "А что с лифтом?"


def test_parse_context_line_maps_summary_to_system() -> None:
    role, text = _parse_context_line("Краткий контекст диалога:\n- Вы: спрашивали про парковку")
    assert role == "system"
    assert text.startswith("Краткий контекст диалога:")




def test_openrouter_assistant_fallback_on_http_400(monkeypatch) -> None:
    provider = AnthropicProvider()

    async def _bad_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad request"}})
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(provider._client, "post", _bad_request)
    reply = asyncio.run(provider.assistant_reply("вопрос про шлагбаум", [], chat_id=1))
    assert "шлагбаум" in reply.lower()
    asyncio.run(provider.aclose())


class _FakeBlock:
    type = "text"
    text = "ok"


class _FakeUsage:
    input_tokens = 5
    output_tokens = 7


class _FakeMessage:
    content = [_FakeBlock()]
    usage = _FakeUsage()


def _patch_completion_env(monkeypatch, provider, create_fn) -> None:
    async def _fake_add_usage(chat_id: int, tokens: int) -> None:
        return None

    async def _allow(chat_id: int) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr("app.services.ai_module.settings.ai_key", "test-key", raising=False)
    monkeypatch.setattr("app.services.ai_module.settings.ai_fallback_model", "claude-haiku-4-5", raising=False)
    # Герметичность: основная модель провайдера фиксируется ОТЛИЧНОЙ от fallback —
    # иначе результат зависит от env AI_MODEL (retry на fallback не происходит,
    # когда основная модель уже равна fallback).
    monkeypatch.setattr(provider, "_model", "claude-test-primary", raising=False)
    monkeypatch.setattr("app.services.ai_module._can_use_remote_ai", _allow)
    monkeypatch.setattr("app.services.ai_module._add_remote_usage", _fake_add_usage)
    monkeypatch.setattr(provider._client.messages, "create", create_fn)


def test_anthropic_retries_with_fallback_model_on_not_found(monkeypatch) -> None:
    import anthropic

    provider = AnthropicProvider()
    sent_models: list[str] = []

    async def _create(**kwargs):  # type: ignore[no-untyped-def]
        sent_models.append(kwargs["model"])
        if len(sent_models) == 1:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            response = httpx.Response(404, request=request)
            raise anthropic.NotFoundError(
                f"model: {kwargs['model']} not found", response=response, body=None
            )
        return _FakeMessage()

    _patch_completion_env(monkeypatch, provider, _create)

    content, tokens = asyncio.run(provider._chat_completion([{"role": "user", "content": "ping"}], chat_id=1))

    assert content == "ok"
    assert tokens == 12
    assert len(sent_models) == 2
    assert sent_models[-1] == "claude-haiku-4-5"
    asyncio.run(provider.aclose())


def test_anthropic_retries_with_fallback_model_on_bad_request(monkeypatch) -> None:
    import anthropic

    provider = AnthropicProvider()
    sent_models: list[str] = []

    async def _create(**kwargs):  # type: ignore[no-untyped-def]
        sent_models.append(kwargs["model"])
        if len(sent_models) == 1:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            response = httpx.Response(400, request=request)
            raise anthropic.BadRequestError(
                f"model {kwargs['model']} is not available", response=response, body=None
            )
        return _FakeMessage()

    _patch_completion_env(monkeypatch, provider, _create)

    content, tokens = asyncio.run(provider._chat_completion([{"role": "user", "content": "ping"}], chat_id=1))

    assert content == "ok"
    assert tokens == 12
    assert len(sent_models) == 2
    assert sent_models[-1] == "claude-haiku-4-5"
    asyncio.run(provider.aclose())

def test_openrouter_summary_fallback_on_runtime_error(monkeypatch) -> None:
    provider = AnthropicProvider()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_chat_completion", _raise)
    result = asyncio.run(provider.generate_daily_summary("ctx", chat_id=1))
    assert result is None
    asyncio.run(provider.aclose())


def test_openrouter_chat_completion_raises_on_empty_content(monkeypatch) -> None:
    provider = AnthropicProvider()

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


def test_static_prompt_exceeds_cache_minimum() -> None:
    """Статичный префикс должен превышать минимум prompt caching Haiku (4096 ток.)."""
    from app.services.ai_module import get_static_assistant_prompt, invalidate_static_prompt_cache

    invalidate_static_prompt_cache()
    prompt = get_static_assistant_prompt()
    # ~1.7 символа/токен для русского: 7500+ символов ≈ 4400+ токенов
    assert len(prompt) > 7500, f"префикс слишком короткий для кэша: {len(prompt)} символов"
    # Все блоки на месте
    assert "<persona_bio>" in prompt
    assert "<humor_guide>" in prompt
    assert "<examples>" in prompt
    assert "<kb_core>" in prompt
    # Базовые правила не потерялись
    assert "Ты — бот-помощник" in prompt
    assert "400 символов" in prompt


def test_static_prompt_is_byte_stable_between_calls() -> None:
    """Байт-в-байт стабильность между вызовами — иначе Anthropic-кэш не сработает."""
    from app.services.ai_module import get_static_assistant_prompt

    assert get_static_assistant_prompt() is get_static_assistant_prompt()


def test_static_prompt_cache_invalidation() -> None:
    from app.services.ai_module import get_static_assistant_prompt, invalidate_static_prompt_cache

    first = get_static_assistant_prompt()
    invalidate_static_prompt_cache()
    second = get_static_assistant_prompt()
    assert first == second  # содержимое то же (KB не менялась)
    assert first is not second  # но объект пересобран


def test_answer_cache_is_isolated_between_chats() -> None:
    """Fuzzy-кэш не должен смешивать чаты: ответ из лог-чата не течёт в форум."""
    from app.services.ai_module import (
        _cache_get, _cache_set, _normalize_cache_key, clear_assistant_cache,
    )

    clear_assistant_cache()
    key_a = f"100|{_normalize_cache_key('как оформить пропуск на гостя машину')}"
    _cache_set(key_a, "ОТВЕТ ИЗ ЧАТА A")

    # Точно тот же вопрос в другом чате — fuzzy не должен вернуть ответ чата A
    key_b = f"200|{_normalize_cache_key('как оформить пропуск на гостя машину')}"
    assert _cache_get(key_b) is None

    # Близкая переформулировка в том же чате — fuzzy обязан сработать
    key_a2 = f"100|{_normalize_cache_key('оформить пропуск на гостя машину быстро')}"
    assert _cache_get(key_a2) == "ОТВЕТ ИЗ ЧАТА A"
    clear_assistant_cache()


def test_social_shortcut_only_for_pure_greetings() -> None:
    """«Привет, телефон УК» — это запрос, дежурную фразу отдавать нельзя."""
    from app.handlers.help import _is_pure_social, _local_social_reply

    assert _is_pure_social("привет")
    assert _is_pure_social("спасибо большое")
    assert _is_pure_social("здравствуй, жабот")
    assert not _is_pure_social("привет, телефон УК")
    assert not _is_pure_social("добрый день, режим работы")

    assert _local_social_reply("привет, телефон УК", 1, 1) is None
    assert _local_social_reply("спасибо большое", 1, 1) is not None


def test_bot_name_called_recognizes_cyrillic_alias() -> None:
    """Жители зовут «Жабот» (кириллица), Telegram-имя — «Jabot» (латиница)."""
    from app.handlers.help import _is_bot_name_called

    class _Profile:
        first_name = "Jabot"
        username = "alexjk_bot"

    prof = _Profile()
    assert _is_bot_name_called("Жабот, привет", prof)
    assert _is_bot_name_called("Жабот", prof)
    assert _is_bot_name_called("Жаб, как дела", prof)
    assert _is_bot_name_called("Jabot, hi", prof)
    assert _is_bot_name_called("бот, помоги", prof)
    assert _is_bot_name_called("Ботик, ты тут?", prof)
    # Не обращение: имя в середине, производные слова
    assert not _is_bot_name_called("спроси у Жабота потом", prof)
    assert not _is_bot_name_called("Жаботина сломалась", prof)
    assert not _is_bot_name_called("боты захватят мир", prof)
    assert not _is_bot_name_called("привет всем", prof)
    # Старое имя «Алекс» убрано — бот только Жабот
    assert not _is_bot_name_called("Алекс, привет", prof)


def test_bot_identity_is_zhabot_everywhere() -> None:
    """Бот представляется только Жаботом — никаких «Алекс» в текстах."""
    from app.handlers.help import _ABILITIES_CONTEXT, HELP_MENU_TEXT

    assert "Жабот" in _ABILITIES_CONTEXT
    assert "Алекс" not in _ABILITIES_CONTEXT
    assert "Жабот" in HELP_MENU_TEXT
    assert "Алекс" not in HELP_MENU_TEXT
    # Справка описывает реальное поведение: как позвать и что на реплаи отвечает всегда
    assert "реплаем" in _ABILITIES_CONTEXT or "реплай" in _ABILITIES_CONTEXT
    assert "только по запросу" in _ABILITIES_CONTEXT.lower()
    # Мёртвых функций в справке нет
    assert "Проактивный" not in _ABILITIES_CONTEXT
    assert "подарить" not in HELP_MENU_TEXT


def test_reactions_fire_only_on_meaningful_messages() -> None:
    """Реакции — только на осмысленный повод, не на «ерунду»."""
    from app.handlers.moderation import _REACT_RULES

    def _matches(text: str) -> bool:
        return any(p.search(text) for p, _ in _REACT_RULES)

    # Повод есть
    assert _matches("Спасибо, сосед, выручил!")
    assert _matches("Поздравляю с новосельем 🎉")
    assert _matches("Наконец-то починили лифт")
    assert _matches("Отличная новость, класс!")
    # Повода нет — бот не лайкает
    assert not _matches("Кто-нибудь знает во сколько завтра отключат воду")
    assert not _matches("Машина у второго подъезда мешает проезду")
    assert not _matches("Сегодня опять пробки на въезде")
