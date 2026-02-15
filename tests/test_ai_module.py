import asyncio

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db import Base, engine
from app.services.ai_module import (
    AiModuleClient,
    detect_profanity,
    is_assistant_topic_allowed,
    local_quiz_answer_decision,
    mask_personal_data,
    normalize_for_profanity,
    parse_quiz_answer_response,
)


async def _prepare_db(async_engine: AsyncEngine) -> None:
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def test_detects_masked_profanity() -> None:
    normalized = normalize_for_profanity("Да ты б*л_я!")
    assert detect_profanity(normalized)


def test_masks_personal_data() -> None:
    masked = mask_personal_data("Иван Иванов, +79991234567, test@example.com")
    assert "+79991234567" not in masked
    assert "test@example.com" not in masked


def test_assistant_topic_restrictions() -> None:
    assert is_assistant_topic_allowed("Как решить проблему со шлагбаумом?")
    assert not is_assistant_topic_allowed("Дай финансовый совет")


def test_parse_quiz_answer_response() -> None:
    decision = parse_quiz_answer_response({"is_correct": True, "confidence": 0.9})
    assert decision.is_correct is True
    assert decision.is_close is True


def test_local_quiz_answer_close_match() -> None:
    decision = local_quiz_answer_decision("домофон в подъезде", "домофон")
    assert decision.is_close is True


def test_probe_returns_success_for_valid_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "secret")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "label": "NONE",
                "severity": 0,
                "confidence": 1,
                "recommended_action": "ALLOW",
            },
        )

    client = AiModuleClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(client.probe())
    assert result.ok is True


def test_probe_maps_unauthorized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "bad")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized"})

    client = AiModuleClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(client.probe())
    assert result.ok is False
    assert "401" in result.details


def test_ai_request_limit(monkeypatch) -> None:
    asyncio.run(_prepare_db(engine))
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "secret")
    monkeypatch.setattr(settings, "ai_daily_request_limit", 1)
    monkeypatch.setattr(settings, "ai_daily_token_limit", 1000)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"reply": "ok"})

    client = AiModuleClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    first = asyncio.run(client.assistant_reply("вопрос про дом", [], chat_id=1))
    second = asyncio.run(client.assistant_reply("вопрос про дом", [], chat_id=1))

    assert "ok" in first
    assert "упрощенный режим" in second


def test_ai_token_limit(monkeypatch) -> None:
    asyncio.run(_prepare_db(engine))
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "secret")
    monkeypatch.setattr(settings, "ai_daily_request_limit", 100)
    monkeypatch.setattr(settings, "ai_daily_token_limit", 10)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"reply": "ok", "usage": {"total_tokens": 11}})

    client = AiModuleClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    _ = asyncio.run(client.assistant_reply("вопрос про дом", [], chat_id=1))
    second = asyncio.run(client.assistant_reply("вопрос про дом", [], chat_id=1))
    assert "упрощенный режим" in second


def test_401_without_retries(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "secret")
    monkeypatch.setattr(settings, "ai_retries", 3)
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    client = AiModuleClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reply = asyncio.run(client.assistant_reply("вопрос про дом", [], chat_id=1))
    assert calls["count"] == 1
    assert "упрощенный режим" in reply


def test_5xx_with_retries(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "secret")
    monkeypatch.setattr(settings, "ai_retries", 2)
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, json={"error": "down"})

    client = AiModuleClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reply = asyncio.run(client.assistant_reply("вопрос про дом", [], chat_id=1))
    assert calls["count"] == 3
    assert "упрощенный режим" in reply
