"""Почему: централизуем конфигурацию из окружения для удобства деплоя и тестов."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    timezone: str = "Europe/Moscow"
    build_version: str = "dev"
    ai_enabled: bool = True
    ai_api_url: str | None = None
    ai_key: str | None = None
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
            cleaned = value.strip()
            if cleaned:
                return cleaned
        raise ValueError("BOT_TOKEN не задан или пуст")


    @property
    def data_dir(self) -> Path:
        """Возвращает директорию для служебных файлов (SQLite или дефолт)."""
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            db_path = Path(self.database_url.removeprefix(prefix))
            return db_path.expanduser().resolve().parent
        return Path("/app/data")


def _load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logger = logging.getLogger(__name__)
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
        logger.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(1) from exc


settings = _load_settings()
