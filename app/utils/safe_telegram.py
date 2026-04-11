"""Почему: любая ошибка Telegram API в модерации не должна ронять хендлер.

Бот должен продолжать работать, а сбой — логироваться и (по возможности)
сообщаться админам, а не превращаться в тихий падеж.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, TypeVar

from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def safe_call(coro: Awaitable[T], *, log_ctx: str) -> T | None:
    """Выполняет корутину Telegram API, глуша исключения.

    Использование:
        await safe_call(bot.restrict_chat_member(...), log_ctx="mute user_id=123")

    Возвращает результат корутины либо None при сбое.
    """
    try:
        return await coro
    except TelegramAPIError as exc:
        logger.warning("Telegram API error [%s]: %s", log_ctx, exc)
    except Exception:  # noqa: BLE001 - любой сбой, включая сетевые
        logger.exception("Неожиданная ошибка при вызове Telegram API [%s].", log_ctx)
    return None
