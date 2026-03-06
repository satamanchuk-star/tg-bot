"""Почему: валидируем критичные ошибки конфигурации заранее, до старта бота."""

from __future__ import annotations

from pydantic import ValidationError

from app.config import Settings, _extract_compose_bot_env


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


def test_extract_compose_bot_env_from_environment_block(tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        """services:
  bot:
    image: test
    environment:
      - BOT_TOKEN=from-compose
      - FORUM_CHAT_ID=-100123
      - ADMIN_LOG_CHAT_ID=-100456
""",
        encoding="utf-8",
    )

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "from-compose"
    assert env["FORUM_CHAT_ID"] == "-100123"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100456"


def test_extract_compose_bot_env_from_env_file(tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=from-env-file\nFORUM_CHAT_ID=-100111\nADMIN_LOG_CHAT_ID=-100222\n",
        encoding="utf-8",
    )
    compose.write_text(
        """services:
  bot:
    image: test
    env_file:
      - .env
""",
        encoding="utf-8",
    )

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "from-env-file"
    assert env["FORUM_CHAT_ID"] == "-100111"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100222"
