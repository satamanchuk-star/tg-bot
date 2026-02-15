"""Почему: валидируем критичные ошибки конфигурации заранее, до старта бота."""

from __future__ import annotations

from pydantic import ValidationError

from app.config import Settings


BASE_ENV: dict[str, str] = {
    "bot_token": "test-token",
    "forum_chat_id": "1",
    "admin_log_chat_id": "1",
}


def test_settings_ignores_empty_optional_values() -> None:
    settings = Settings(
        **BASE_ENV,
        _env_file=None,
        AI_API_URL="",
        TOPIC_GAMES="",
    )

    assert settings.ai_api_url is None
    assert settings.topic_games is None


def test_settings_rejects_empty_bot_token(monkeypatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("FORUM_CHAT_ID", raising=False)
    monkeypatch.delenv("ADMIN_LOG_CHAT_ID", raising=False)

    try:
        Settings(
            _env_file=None,
            bot_token="   ",
            forum_chat_id="1",
            admin_log_chat_id="1",
        )
    except ValidationError as exc:
        assert "BOT_TOKEN не задан или пуст" in str(exc)
    else:
        raise AssertionError("Expected ValidationError for empty BOT_TOKEN")


def test_settings_reads_openrouter_api_key_alias() -> None:
    settings = Settings(
        **BASE_ENV,
        _env_file=None,
        OPENROUTER_API_KEY="or-test-key",
    )

    assert settings.ai_key == "or-test-key"
