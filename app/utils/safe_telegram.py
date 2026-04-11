"""Почему: ошибки Telegram API не должны прерывать обработчики и джобы."""

from __future__ import annotations

import logging
from typing import Awaitable, TypeVar

from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def safe_call(coro: Awaitable[T], *, log_ctx: str) -> T | None:
    """Выполняет Telegram-вызов с мягкой обработкой ошибок.

    Возвращает результат корутины или ``None`` при ошибке.
    """
    try:
        return await coro
    except TelegramAPIError as exc:
        logger.warning("Telegram API error [%s]: %s", log_ctx, exc)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected Telegram call error [%s].", log_ctx)
    return None
