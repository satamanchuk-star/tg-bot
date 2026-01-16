"""Почему: централизуем конфигурацию из окружения для удобства деплоя и тестов."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения, читаются из переменных окружения."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    forum_chat_id: int
    admin_log_chat_id: int
    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    timezone: str = "Europe/Moscow"

    topic_rules: int
    topic_important: int
    topic_buildings_41_42: int
    topic_building_2: int
    topic_building_3: int
    topic_complaints: int
    topic_rides: int
    topic_smoke: int
    topic_pets: int
    topic_repair: int
    topic_realty: int
    topic_parents: int
    topic_ads: int
    topic_games: int
    topic_gate: int
    topic_services: int
    topic_uk: int
    topic_neighbors: int
    topic_market: int
    topic_duplex: int


settings = Settings()  # type: ignore[call-arg]
