"""Почему: централизуем конфигурацию из окружения для удобства деплоя и тестов."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


SERVER_COMPOSE_PATHS: tuple[Path, ...] = (
    Path("/opt/alexbot/docker-compose.yaml"),
    Path("/opt/alexbot/docker-compose.yml"),
)
REQUIRED_ENV_FIELDS: tuple[str, ...] = (
    "BOT_TOKEN",
    "FORUM_CHAT_ID",
    "ADMIN_LOG_CHAT_ID",
)


def _has_required_bot_env(env: dict[str, str]) -> bool:
    """Проверяет наличие обязательных переменных бота."""
    return all(env.get(field) for field in REQUIRED_ENV_FIELDS)


def _resolve_compose_value(raw_value: str) -> str:
    """Упрощённо резолвит ${VAR} и ${VAR:-default} из docker-compose."""
    value = raw_value.strip().strip("'\"")
    if not (value.startswith("${") and value.endswith("}")):
        return value

    inner = value[2:-1]
    if ":-" in inner:
        env_name, default = inner.split(":-", 1)
        return os.getenv(env_name, default)
    return os.getenv(inner, "")


def _read_env_file_values(compose_path: Path, env_file_item: str) -> dict[str, str]:
    """Читает переменные из env_file, если файл существует."""
    env_file_candidate = Path(env_file_item).expanduser()
    if env_file_candidate.is_absolute():
        env_file_path = env_file_candidate.resolve()
    else:
        env_file_path = (compose_path.parent / env_file_candidate).resolve()
    if not env_file_path.exists():
        return {}

    env_values = dotenv_values(env_file_path)
    result: dict[str, str] = {}
    for key, value in env_values.items():
        if value is None:
            continue
        result[str(key)] = str(value)
    return result


def _extract_compose_service_env(compose_path: Path, service_name: str) -> dict[str, str]:
    """Извлекает environment/env_file для указанного сервиса из docker-compose."""
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

        if stripped == f"{service_name}:" and indent == 2:
            in_bot = True
            in_environment = False
            in_env_file = False
            continue

        if in_bot and indent <= 2 and stripped.endswith(":") and stripped != f"{service_name}:":
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

        if stripped.startswith("env_file:") and indent == 4:
            env_file_item = stripped.partition(":")[2].strip().strip("'\"")
            if env_file_item:
                result.update(_read_env_file_values(compose_path, env_file_item))
            in_env_file = False
            in_environment = False
            continue

        if indent == 4 and stripped.endswith(":"):
            in_environment = False
            in_env_file = False

        if in_environment:
            if indent == 6 and "=" in stripped and stripped.startswith("-"):
                item = stripped.removeprefix("-").strip()
                key, value = item.split("=", 1)
                result[key.strip()] = _resolve_compose_value(value)
                continue
            if indent == 6 and stripped.startswith("-") and "=" not in stripped:
                key = stripped.removeprefix("-").strip()
                env_value = os.getenv(key)
                if env_value is not None:
                    result[key] = env_value
                continue
            if indent == 6 and ":" in stripped and not stripped.startswith("-"):
                key, value = stripped.split(":", 1)
                key = key.strip()
                resolved = _resolve_compose_value(value)
                if resolved:
                    result[key] = resolved
                elif key in os.environ:
                    result[key] = os.environ[key]
                continue

        if in_env_file and indent == 6 and stripped.startswith("-"):
            env_file_item = stripped.removeprefix("-").strip()
            if env_file_item.startswith("path:"):
                env_file_path = env_file_item.partition(":")[2].strip().strip("'\"")
                if env_file_path:
                    result.update(_read_env_file_values(compose_path, env_file_path))
                continue
            env_file_path = env_file_item.strip("'\"")
            if env_file_path:
                result.update(_read_env_file_values(compose_path, env_file_path))

    return result


def _extract_compose_bot_env(compose_path: Path) -> dict[str, str]:
    """Извлекает env для целевого сервиса бота (bot/alexbot или первого валидного)."""
    if not compose_path.exists():
        return {}

    lines = compose_path.read_text(encoding="utf-8").splitlines()
    services: list[str] = []
    in_services = False
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "services:" and indent == 0:
            in_services = True
            continue
        if in_services and indent == 0 and stripped.endswith(":") and stripped != "services:":
            in_services = False
        if in_services and indent == 2 and stripped.endswith(":"):
            services.append(stripped[:-1].strip())

    preferred = ["bot", "alexbot"]
    checked: list[str] = []
    best_match: dict[str, str] = {}

    for service in [*preferred, *services]:
        if service in checked:
            continue
        checked.append(service)
        env = _extract_compose_service_env(compose_path, service)
        if not env:
            continue
        if _has_required_bot_env(env):
            return env
        if len(env) > len(best_match):
            best_match = env
    return best_match


def _inject_env_from_server_compose() -> None:
    """Подхватывает env из /opt/alexbot/docker-compose.yaml как источник истины."""
    compose_env: dict[str, str] = {}
    fallback_env: dict[str, str] = {}
    for compose_path in SERVER_COMPOSE_PATHS:
        compose_env = _extract_compose_bot_env(compose_path)
        if not compose_env:
            continue
        if _has_required_bot_env(compose_env):
            break
        if len(compose_env) > len(fallback_env):
            fallback_env = compose_env
    if not _has_required_bot_env(compose_env):
        compose_env = fallback_env
    for key, value in compose_env.items():
        if not value:
            continue
        existing = os.getenv(key)
        if existing:
            continue
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

    # Прокси для доступа к Telegram API.
    # Поддерживается либо автоподбор HTTP/SOCKS-прокси из публичных GitHub-списков,
    # либо ручной адрес через PROXY_MANUAL (имеет приоритет).
    proxy_enabled: bool = False
    proxy_refresh_interval_min: int = 30
    proxy_manual: str | None = None  # напр. socks5://user:pass@host:1080
    proxy_working_pool_size: int = 5
    proxy_test_limit: int = 500
    proxy_state_path: str = "data/working_proxies.json"

    ai_enabled: bool = True
    ai_api_url: str | None = None
    ai_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_KEY", "OPENROUTER_API_KEY", "AI_API_KEY"),
    )
    ai_model: str = Field(
        default="anthropic/claude-haiku-4.5",
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
    ai_feature_daily_summary: bool = True
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
    # Тихое обучение модерации: бот НЕ модерирует, а отправляет подозрительные
    # сообщения в лог-чат с кнопками для подтверждения действия администратором.
    moderation_training_mode: bool = False
    # Плановые приветствия в форуме
    ai_greeting_topic_id: int | None = None  # Топик для утреннего/вечернего приветствия
    ai_morning_greeting: bool = False  # Включить утреннее приветствие (9:00)
    ai_evening_greeting: bool = False  # Включить вечернее приветствие (20:00)
    # Утреннее приветствие с погодой и праздниками (8:00 в General)
    ai_daily_greeting: bool = False
    ai_summary_hour: int = 21
    ai_summary_minute: int = 0
    ai_summary_topic_id: int | None = None
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

    @property
    def ai_model_is_default(self) -> bool:
        """Показывает, был ли AI_MODEL задан явно через окружение/.env."""
        return "ai_model" not in self.model_fields_set


def _load_settings() -> Settings:
    _inject_env_from_server_compose()
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
                "На сервере источник переменных: /opt/alexbot/docker-compose.yaml (bot.environment/env_file).",
            )
        logger.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(1) from exc


settings = _load_settings()
