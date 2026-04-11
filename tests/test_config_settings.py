"""Почему: валидируем критичные ошибки конфигурации заранее, до старта бота."""

from __future__ import annotations

import os

from pydantic import ValidationError

from app.config import (
    Settings,
    _extract_compose_bot_env,
    _inject_env_from_server_compose,
)


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


def test_extract_compose_bot_env_from_inline_env_file(tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=inline-env-file\nFORUM_CHAT_ID=-100010\nADMIN_LOG_CHAT_ID=-100020\n",
        encoding="utf-8",
    )
    compose.write_text(
        """services:
  bot:
    image: test
    env_file: .env
""",
        encoding="utf-8",
    )

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "inline-env-file"
    assert env["FORUM_CHAT_ID"] == "-100010"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100020"


def test_extract_compose_bot_env_resolves_compose_placeholders(monkeypatch, tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        """services:
  bot:
    image: test
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      FORUM_CHAT_ID: ${FORUM_CHAT_ID:-1}
      ADMIN_LOG_CHAT_ID: ${ADMIN_LOG_CHAT_ID}
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("BOT_TOKEN", "from-process-env")
    monkeypatch.setenv("ADMIN_LOG_CHAT_ID", "-100777")
    monkeypatch.delenv("FORUM_CHAT_ID", raising=False)

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "from-process-env"
    assert env["FORUM_CHAT_ID"] == "1"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100777"


def test_inject_env_from_server_compose_keeps_existing_process_env(monkeypatch, tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        """services:
  bot:
    image: test
    environment:
      - BOT_TOKEN=from-compose
      - FORUM_CHAT_ID=-100123
      - ADMIN_LOG_CHAT_ID=-100456
      - AI_MODEL=qwen/qwen3.5-flash
""",
        encoding="utf-8",
    )

    monkeypatch.setattr("app.config.SERVER_COMPOSE_PATHS", (compose,))
    monkeypatch.setenv("BOT_TOKEN", "from-env")
    monkeypatch.delenv("FORUM_CHAT_ID", raising=False)
    monkeypatch.delenv("ADMIN_LOG_CHAT_ID", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)

    _inject_env_from_server_compose()

    assert os.environ["BOT_TOKEN"] == "from-env"
    assert os.environ["FORUM_CHAT_ID"] == "-100123"
    assert os.environ["ADMIN_LOG_CHAT_ID"] == "-100456"
    assert os.environ["AI_MODEL"] == "qwen/qwen3.5-flash"


def test_inject_env_from_server_compose_skips_empty_values(monkeypatch, tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        """services:
  bot:
    image: test
    environment:
      AI_KEY: ${AI_KEY}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr("app.config.SERVER_COMPOSE_PATHS", (compose,))
    monkeypatch.delenv("AI_KEY", raising=False)

    _inject_env_from_server_compose()

    assert "AI_KEY" not in os.environ


def test_inject_env_from_server_compose_fallback_to_yml(monkeypatch, tmp_path) -> None:
    yaml_path = tmp_path / "docker-compose.yaml"
    yml_path = tmp_path / "docker-compose.yml"
    yaml_path.write_text("services: {}\n", encoding="utf-8")
    yml_path.write_text(
        """services:
  bot:
    image: test
    environment:
      - BOT_TOKEN=from-yml
      - FORUM_CHAT_ID=-100001
      - ADMIN_LOG_CHAT_ID=-100002
""",
        encoding="utf-8",
    )

    monkeypatch.setattr("app.config.SERVER_COMPOSE_PATHS", (yaml_path, yml_path))
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("FORUM_CHAT_ID", raising=False)
    monkeypatch.delenv("ADMIN_LOG_CHAT_ID", raising=False)

    _inject_env_from_server_compose()

    assert os.environ["BOT_TOKEN"] == "from-yml"
    assert os.environ["FORUM_CHAT_ID"] == "-100001"
    assert os.environ["ADMIN_LOG_CHAT_ID"] == "-100002"


def test_extract_compose_bot_env_from_env_file_path_mapping(tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=from-path-mapping\nFORUM_CHAT_ID=-100301\nADMIN_LOG_CHAT_ID=-100302\n",
        encoding="utf-8",
    )
    compose.write_text(
        """services:
  bot:
    image: test
    env_file:
      - path: .env
        required: true
""",
        encoding="utf-8",
    )

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "from-path-mapping"
    assert env["FORUM_CHAT_ID"] == "-100301"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100302"


def test_extract_compose_bot_env_supports_alexbot_service_name(tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        """services:
  alexbot:
    image: test
    environment:
      - BOT_TOKEN=from-alexbot
      - FORUM_CHAT_ID=-100901
      - ADMIN_LOG_CHAT_ID=-100902
""",
        encoding="utf-8",
    )

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "from-alexbot"
    assert env["FORUM_CHAT_ID"] == "-100901"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100902"


def test_extract_compose_bot_env_finds_first_service_with_required_vars(tmp_path) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        """services:
  watchtower:
    image: containrrr/watchtower
    environment:
      - WATCHTOWER_CLEANUP=true
  resident_bot:
    image: test
    environment:
      - BOT_TOKEN=from-resident-bot
      - FORUM_CHAT_ID=-100991
      - ADMIN_LOG_CHAT_ID=-100992
""",
        encoding="utf-8",
    )

    env = _extract_compose_bot_env(compose)

    assert env["BOT_TOKEN"] == "from-resident-bot"
    assert env["FORUM_CHAT_ID"] == "-100991"
    assert env["ADMIN_LOG_CHAT_ID"] == "-100992"
