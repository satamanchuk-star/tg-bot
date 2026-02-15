import asyncio

from app.config import settings
from app.services.ai_module import AiModuleClient


def test_daily_summary_ai_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_api_url", "https://ai.example.test")
    monkeypatch.setattr(settings, "ai_key", "secret")
    monkeypatch.setattr(settings, "ai_feature_daily_summary", True)

    async def fake_run_ai(self, *, payload, chat_id, operation):
        return type("Result", (), {"ok": True, "data": {"reply": "итог дня"}, "reason": "ok"})

    monkeypatch.setattr(AiModuleClient, "_run_ai", fake_run_ai)
    client = AiModuleClient()
    result = asyncio.run(client.generate_daily_summary("контекст", chat_id=1))
    assert result == "итог дня"


def test_daily_summary_fallback_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_feature_daily_summary", False)
    client = AiModuleClient()
    result = asyncio.run(client.generate_daily_summary("контекст", chat_id=1))
    assert result is None
