"""Почему: централизуем конфигурацию из окружения для удобства деплоя и тестов.

Секреты (BOT_TOKEN, ANTHROPIC_API_KEY и т.п.) приходят ТОЛЬКО из окружения /
файла `.env`, который деплой-пайплайн пишет на сервере из GitHub Secrets.
Никакого чтения секретов из docker-compose на сервере больше нет.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import AliasChoices, Field, ValidationError, field_validator
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
    # Опциональный override эндпоинта Anthropic (например, корпоративный прокси).
    # Пусто → официальный https://api.anthropic.com через anthropic SDK.
    ai_api_url: str | None = None
    ai_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "AI_KEY", "AI_API_KEY"),
    )
    ai_model: str = Field(
        default="claude-haiku-4-5",
        validation_alias=AliasChoices("AI_MODEL", "ai_model"),
    )
    ai_max_tokens: int = 800
    ai_timeout_seconds: int = 12
    ai_retries: int = 1
    ai_daily_request_limit: int = 2000
    ai_daily_token_limit: int = 400000
    ai_feature_moderation: bool = True
    ai_feature_assistant: bool = True
    ai_feature_web_search: bool = True
    # «Отвечать реже, но точнее»: на фактический вопрос без опоры в базе знаний
    # (KB/RAG/FAQ/places/web) бот честно говорит «не знаю» вместо генерации догадки.
    # Болтовня/приветствия этим гейтом не затрагиваются.
    ai_require_grounding: bool = True
    # Проактивный режим: бот сам подсказывает, когда может помочь
    ai_feature_proactive: bool = True
    # Профили жителей: бот запоминает факты о пользователях из диалогов
    ai_feature_profiles: bool = True
    # Еженедельные персональные косания в DM по фактам из профиля. Off-by-default:
    # требует, чтобы пользователь ранее писал боту в личку (иначе TelegramForbidden).
    ai_feature_weekly_nudge: bool = False
    weekly_nudge_max_per_run: int = 20  # верхний лимит DM за один запуск джобы
    weekly_nudge_min_days_between: int = 30  # одному пользователю — не чаще раза в N дней
    # Адаптация тона под настроение чата
    ai_feature_mood: bool = True

    # --- Multi-model routing (Anthropic Claude) ---
    # По умолчанию всё на дешёвом Claude Haiku; Sonnet — только премиум-путь.
    ai_classifier_model: str = "claude-haiku-4-5"
    ai_spam_model: str = "claude-haiku-4-5"
    ai_topic_model: str = "claude-haiku-4-5"
    ai_gate_intent_model: str = "claude-haiku-4-5"
    ai_main_model: str = "claude-haiku-4-5"
    ai_faq_model: str = "claude-haiku-4-5"
    ai_reply_model: str = "claude-haiku-4-5"
    ai_digest_model: str = "claude-haiku-4-5"
    ai_gate_extract_model: str = "claude-haiku-4-5"
    ai_code_model: str = "claude-haiku-4-5"
    # Премиум-ответы (крайние случаи) — Claude Sonnet.
    ai_premium_model: str = "claude-sonnet-4-6"
    ai_fallback_model: str = "claude-haiku-4-5"

    # --- Extended limits ---
    ai_max_daily_cost_usd: float = 2.0
    ai_classifier_max_output_tokens: int = 120
    ai_reply_max_output_tokens: int = 500
    ai_digest_max_output_tokens: int = 700

    # Тихое обучение модерации: бот НЕ модерирует, а отправляет подозрительные
    # сообщения в лог-чат с кнопками для подтверждения действия администратором.
    moderation_training_mode: bool = False
    # Утреннее приветствие с погодой и праздниками (8:00 в General)
    ai_daily_greeting: bool = False
    db_logs_retention_days: int = 14
    db_stats_retention_days: int = 45
    google_sheets_spreadsheet_id: str = "1OsPh54Bn5fdkfsEJyKcbYZTcHnue3EWQ"
    google_sheets_worksheet_name: str = "Objects"
    google_service_account_file: str | None = None

    topic_rules: int | None = None
    topic_important: int | None = None
    topic_buildings_41_42: int | None = None
    topic_building_2: int | None = None
    topic_building_3: int | None = None
    topic_complaints: int | None = None
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
    # Топик «Попутчики»: бот не отвечает (по запросу пользователя),
    # но модерация продолжает работать.
    topic_rides: int | None = None

    @field_validator(
        "topic_rules",
        "topic_important",
        "topic_buildings_41_42",
        "topic_building_2",
        "topic_building_3",
        "topic_complaints",
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
        "topic_rides",
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

    @field_validator("ai_model", mode="before")
    @classmethod
    def _normalize_ai_model(cls, value: object) -> object:
        """Исправляет частую опечатку `qwen3,5` -> `qwen3.5` в AI_MODEL."""
        if not isinstance(value, str):
            return value
        normalized = value.strip().strip("'\"")
        if "," in normalized:
            normalized = normalized.replace(",", ".")
        return normalized


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
                "Источник переменных: файл .env (на сервере его пишет деплой из GitHub Secrets).",
            )
        logger.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(1) from exc


settings = _load_settings()
