"""Почему: проверяем отказоустойчивость AI-провайдера при ошибках сети/таймаутах."""

from __future__ import annotations

import asyncio

import httpx

from app.services.ai_module import OpenRouterProvider


def test_assistant_reply_falls_back_on_timeout(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(provider._client, "post", _timeout)
    reply = asyncio.run(provider.assistant_reply("как проехать через шлагбаум?", [], chat_id=1))
    assert isinstance(reply, str)
    assert len(reply.strip()) > 0
    asyncio.run(provider.aclose())


def test_assistant_reply_falls_back_on_http_error(monkeypatch) -> None:
    provider = OpenRouterProvider()

    async def _bad_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(503, request=request, json={"error": {"message": "unavailable"}})
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr(provider._client, "post", _bad_request)
    reply = asyncio.run(provider.assistant_reply("что с парковкой?", [], chat_id=1))
    assert isinstance(reply, str)
    assert len(reply.strip()) > 0
    asyncio.run(provider.aclose())
