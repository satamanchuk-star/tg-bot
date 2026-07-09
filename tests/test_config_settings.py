"""Почему: валидируем критичные ошибки конфигурации заранее, до старта бота.

Секреты приходят только из окружения / .env — отдельного парсера docker-compose
больше нет (его убрали при переходе на secrets-only деплой).
"""

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


def test_settings_reads_anthropic_api_key_alias() -> None:
    settings = Settings(
        **BASE_ENV,
        _env_file=None,
        ANTHROPIC_API_KEY="sk-ant-test-key",
    )

    assert settings.ai_key == "sk-ant-test-key"



def test_settings_reads_ai_api_key_alias() -> None:
    settings = Settings(
        **BASE_ENV,
        _env_file=None,
        AI_API_KEY="legacy-test-key",
    )

    assert settings.ai_key == "legacy-test-key"


def test_settings_normalizes_ai_model_decimal_separator() -> None:
    settings = Settings(
        **BASE_ENV,
        _env_file=None,
        AI_MODEL="qwen/qwen3,5-flash",
    )

    assert settings.ai_model == "qwen/qwen3.5-flash"


def test_settings_defaults_to_claude_haiku(monkeypatch) -> None:
    for var in ("AI_MODEL", "AI_PREMIUM_MODEL", "AI_FALLBACK_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(**BASE_ENV, _env_file=None)
    assert settings.ai_model == "claude-haiku-4-5"
    assert settings.ai_premium_model == "claude-sonnet-4-6"
