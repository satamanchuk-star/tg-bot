import asyncio

from app.services.ai_module import AiModuleClient


def test_daily_summary_returns_none_in_stub_mode() -> None:
    result = asyncio.run(AiModuleClient().generate_daily_summary("контекст", chat_id=1))
    assert result is None
