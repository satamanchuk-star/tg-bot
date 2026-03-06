"""Почему: централизуем конфигурацию из окружения для удобства деплоя и тестов."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


REQUIRED_ENV_FIELDS: tuple[str, ...] = (
    "BOT_TOKEN",
    "FORUM_CHAT_ID",
    "ADMIN_LOG_CHAT_ID",
)


def _extract_compose_bot_env(compose_path: Path) -> dict[str, str]:
    """Извлекает environment/env_file для сервиса bot из docker-compose.yaml."""
    if not compose_path.exists():
        return {}

    lines = compose_path.read_text(encoding="utf-8").splitlines()
    result: dict[str, str] = {}

    in_bot = False
    in_environment = False
    in_env_file = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        if not stripped or stripped.startswith("#"):
            continue

        if stripped == "bot:" and indent == 2:
            in_bot = True
            in_environment = False
            in_env_file = False
            continue

        if in_bot and indent <= 2 and stripped.endswith(":") and stripped != "bot:":
            in_bot = False
            in_environment = False
            in_env_file = False

        if not in_bot:
            continue

        if stripped == "environment:" and indent == 4:
            in_environment = True
            in_env_file = False
            continue

        if stripped == "env_file:" and indent == 4:
            in_env_file = True
            in_environment = False
            continue

        if indent == 4 and stripped.endswith(":"):
            in_environment = False
            in_env_file = False

        if in_environment:
            if indent == 6 and "=" in stripped and stripped.startswith("-"):
                item = stripped.removeprefix("-").strip()
                key, value = item.split("=", 1)
                result[key.strip()] = value.strip().strip("'\"")
                continue
            if indent == 6 and ":" in stripped and not stripped.startswith("-"):
                key, value = stripped.split(":", 1)
                result[key.strip()] = value.strip().strip("'\"")
                continue

        if in_env_file and indent == 6 and stripped.startswith("-"):
            env_file_item = stripped.removeprefix("-").strip().strip("'\"")
            env_file_path = (compose_path.parent / env_file_item).resolve()
            if env_file_path.exists():
                env_values = dotenv_values(env_file_path)
                for key, value in env_values.items():
                    if value is None:
                        continue
                    result[str(key)] = str(value)

    return result


def _inject_required_env_from_server_compose() -> None:
    """Подхватывает обязательные env из /opt/alexbot/docker-compose.yaml, если их нет."""
    compose_path = Path("/opt/alexbot/docker-compose.yaml")
    compose_env = _extract_compose_bot_env(compose_path)
    for key in REQUIRED_ENV_FIELDS:
        if os.getenv(key):
            continue
        value = compose_env.get(key)
        if value:
            os.environ[key] = value


class Settings(BaseSettings):
    """Настройки приложения, читаются из переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
    )

    bot_token: str
    forum_chat_id: int
    admin_log_chat_id: int
    database_url: str = "sqlite+aiosqlite:///app/data/bot.db"

    @field_validator("database_url", mode="before")
    @classmethod
    def _fix_sqlite_relative_path(cls, value: object) -> object:
        """В Docker WORKDIR=/app относительный путь app/data/ резолвится
        в /app/app/data/, минуя том /app/data/. Конвертируем в абсолютный."""
        if not isinstance(value, str):
            return value
        old = "sqlite+aiosqlite:///app/data/"
        if not value.startswith(old):
            return value
        # Конвертируем только в Docker (WORKDIR=/app)
        if Path.cwd() == Path("/app"):
            new = "sqlite+aiosqlite:////app/data/"
            return new + value[len(old):]
        return value

    timezone: str = "Europe/Moscow"
    build_version: str = "dev"
    ai_enabled: bool = True
    ai_api_url: str | None = None
    ai_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_KEY", "OPENROUTER_API_KEY"),
    )
    ai_model: str = "qwen/qwen3-14b"
    ai_timeout_seconds: int = 20
    ai_retries: int = 2
    ai_daily_request_limit: int = 2000
    ai_daily_token_limit: int = 400000
    ai_feature_moderation: bool = True
    ai_feature_assistant: bool = True
    ai_feature_quiz: bool = True
    ai_feature_daily_summary: bool = True
    ai_summary_hour: int = 21
    ai_summary_minute: int = 0
    ai_summary_topic_id: int | None = None
    db_logs_retention_days: int = 14
    db_stats_retention_days: int = 45

    topic_rules: int | None = None
    topic_important: int | None = None
    topic_buildings_41_42: int | None = None
    topic_building_2: int | None = None
    topic_building_3: int | None = None
    topic_complaints: int | None = None
    topic_rides: int | None = None
    topic_smoke: int | None = None
    topic_pets: int | None = None
    topic_repair: int | None = None
    topic_realty: int | None = None
    topic_parents: int | None = None
    topic_ads: int | None = None
    topic_games: int | None = None
    topic_gate: int | None = None
    topic_services: int | None = None
    topic_uk: int | None = None
    topic_neighbors: int | None = None
    topic_market: int | None = None
    topic_duplex: int | None = None

    @field_validator(
        "topic_rules",
        "topic_important",
        "topic_buildings_41_42",
        "topic_building_2",
        "topic_building_3",
        "topic_complaints",
        "topic_rides",
        "topic_smoke",
        "topic_pets",
        "topic_repair",
        "topic_realty",
        "topic_parents",
        "topic_ads",
        "topic_games",
        "topic_gate",
        "topic_services",
        "topic_uk",
        "topic_neighbors",
        "topic_market",
        "topic_duplex",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("bot_token", mode="before")
    @classmethod
    def _validate_bot_token(cls, value: object) -> object:
        if isinstance(value, str):
            # Docker env_file не убирает кавычки — чистим вручную
            cleaned = value.strip().strip("'\"")
            if cleaned:
                return cleaned
        raise ValueError("BOT_TOKEN не задан или пуст")

    @field_validator("ai_key", mode="before")
    @classmethod
    def _clean_ai_key(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip().strip("'\"")
            return cleaned if cleaned else None
        return value


    @property
    def data_dir(self) -> Path:
        """Возвращает директорию для служебных файлов (SQLite или дефолт)."""
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            db_path = Path(self.database_url.removeprefix(prefix))
            return db_path.expanduser().resolve().parent
        return Path("/app/data")


def _load_settings() -> Settings:
    _inject_required_env_from_server_compose()
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        env_file_path = Path(".env").resolve()
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logger = logging.getLogger(__name__)
        logger.error(
            "Проверка .env: path=%s exists=%s cwd=%s",
            env_file_path,
            env_file_path.exists(),
            Path.cwd(),
        )
        missing = []
        for err in exc.errors():
            if err.get("type") != "missing":
                continue
            loc = err.get("loc", [])
            field_name = ".".join(map(str, loc))
            missing.append(field_name.upper())
        if missing:
            logger.error(
                "Не заданы обязательные переменные окружения: %s",
                ", ".join(missing),
            )
            logger.error(
                "Проверьте, что переменные переданы в контейнер через .env или docker-compose.yml/docker-compose.yaml (секция environment/env_file).",
            )
        logger.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(1) from exc


settings = _load_settings()
