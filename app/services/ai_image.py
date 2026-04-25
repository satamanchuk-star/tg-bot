"""Генерация картинок через OpenRouter image-модели. Только для администраторов."""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.db import get_session
from app.services.ai_module import get_ai_client, get_admin_notifier
from app.utils.time import now_tz

logger = logging.getLogger(__name__)

# Примерная стоимость одной генерации (Gemini 2.5 Flash Image via OpenRouter)
_ESTIMATED_IMAGE_COST_USD: float = 0.05

# ---------------------------------------------------------------------------
# Prompt wrappers для каждой команды
# ---------------------------------------------------------------------------

IMAGE_PROMPT_WRAPPERS: dict[str, str] = {
    "meme": (
        "Create a friendly Telegram meme about life in a residential complex (ЖК). "
        "Warm, lighthearted humor. No politics, no 18+ content, no real faces, no personal data. "
        "Large short text if needed. Topic: {prompt}"
    ),
    "poster": (
        "Create a clean event poster for a residential complex. Modern style, large headline, "
        "space for date and time. Style of a modern residential complex. Topic: {prompt}"
    ),
    "digest": (
        "Create an illustration for a daily community news digest. Light humor, "
        "several scenes from courtyard life. Topic: {prompt}"
    ),
    "rules": (
        "Create a friendly illustration of a chat rule for a residential complex community. "
        "Topic: {prompt}"
    ),
    "welcome": (
        "Create a warm welcome card for new neighbors in a residential complex. Topic: {prompt}"
    ),
    "warning": (
        "Create a noticeable but non-aggressive warning image for a residential complex chat. "
        "Topic: {prompt}"
    ),
}

# ---------------------------------------------------------------------------
# Дневной счётчик (in-memory + DB-персистентность)
# ---------------------------------------------------------------------------

_image_count: dict[str, int] = {}  # {"2026-04-25": 3}


def _today_key() -> str:
    return now_tz().date().isoformat()


def get_today_count() -> int:
    return _image_count.get(_today_key(), 0)


def get_today_cost_usd() -> float:
    return get_today_count() * _ESTIMATED_IMAGE_COST_USD


def is_daily_limit_reached() -> bool:
    count = get_today_count()
    if count >= settings.ai_image_daily_limit:
        return True
    if get_today_cost_usd() >= settings.ai_image_max_daily_cost_usd:
        return True
    return False


def _increment_count() -> None:
    key = _today_key()
    _image_count[key] = _image_count.get(key, 0) + 1
    for old_key in list(_image_count):
        if old_key != key:
            del _image_count[old_key]


async def sync_image_count() -> None:
    """Инициализирует in-memory счётчик из БД — вызывается при старте бота."""
    try:
        async for session in get_session():
            from app.services.ai_usage import get_today_image_count
            count = await get_today_image_count(session)
            if count > 0:
                _image_count[_today_key()] = count
                logger.info("Image count restored from DB: %d", count)
            break
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось загрузить счётчик картинок из БД.")


async def _persist_count() -> None:
    """Записывает текущий счётчик в БД (фоново, не блокирует генерацию)."""
    try:
        async for session in get_session():
            from app.services.ai_usage import add_image_usage
            await add_image_usage(session)
            break
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось сохранить счётчик картинок в БД.")


# ---------------------------------------------------------------------------
# Основная функция генерации
# ---------------------------------------------------------------------------

def build_image_prompt(command: str, user_text: str) -> str:
    template = IMAGE_PROMPT_WRAPPERS.get(command, "Image about: {prompt}")
    return template.format(prompt=user_text)


async def generate_image(command: str, user_text: str, *, chat_id: int, user_id: int | None = None) -> bytes:
    """Генерирует картинку, возвращает bytes. При ошибке поднимает RuntimeError."""
    model = settings.ai_image_model
    prompt = build_image_prompt(command, user_text)

    client = get_ai_client()
    provider = client._provider  # noqa: SLF001
    from app.services.ai_module import OpenRouterProvider
    if not isinstance(provider, OpenRouterProvider):
        raise RuntimeError("AI в stub-режиме, генерация картинок недоступна")

    logger.info("AI image generate command=%s model=%s chat_id=%s", command, model, chat_id)
    try:
        image_bytes = await provider.generate_image_raw(model, prompt, chat_id=chat_id)
        _increment_count()
        asyncio.create_task(_persist_count())

        notifier = get_admin_notifier()
        if notifier:
            try:
                username_hint = f"user_id={user_id}" if user_id else "unknown"
                await notifier(
                    f"🖼 AI image generated\n"
                    f"Command: /{command}\nModel: {model}\n"
                    f"Admin: {username_hint}\nPrompt chars: {len(user_text)}"
                )
            except Exception:  # noqa: BLE001
                pass

        return image_bytes
    except Exception as exc:
        logger.warning("AI image generation error: %s", exc)

        notifier = get_admin_notifier()
        if notifier:
            try:
                await notifier(f"⚠️ AI image error\nCommand: /{command}\nModel: {model}\nError: {exc!s:.200}")
            except Exception:  # noqa: BLE001
                pass

        raise RuntimeError(f"Ошибка генерации картинки: {exc}") from exc
