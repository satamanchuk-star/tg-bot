"""Почему: общие примитивы игр в теме topic_games («21», викторина).

Обе игры повторяли одни и те же куски: реестр asyncio-локов, гейт «мы в теме
игр», глотающие транзиентные ошибки send/edit/answer/react, медали лидерборда.
Одна реализация — одна семантика (важно для swallow-логики ошибок Telegram).
Новые игры подключаются к этим же примитивам.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import settings


class LockRegistry:
    """Реестр asyncio-локов по ключу (user_id или chat_id).

    Словарь не чистим: рост O(число игравших за аптайм) — десятки записей,
    приемлемо (то же соглашение, что было в обоих хендлерах).
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def for_key(self, key: int) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def clear(self) -> None:
        """Сброс всех локов (используется тестами между сценариями)."""
        self._locks.clear()


def in_games_topic(message: Message) -> bool:
    """Сообщение пришло в тему игр форума (topic_games задан и совпал)."""
    return (
        settings.topic_games is not None
        and message.chat.id == settings.forum_chat_id
        and message.message_thread_id == settings.topic_games
    )


def display_name(message: Message) -> str | None:
    if message.from_user is None:
        return None
    return message.from_user.username or message.from_user.full_name


def medal(place: int) -> str:
    """Значок места: медали для топ-3, номер для остальных."""
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")


async def safe_send(bot: Bot, text: str) -> Message | None:
    """Отправка в тему игр; транзиентные ошибки Telegram — молча None."""
    try:
        return await bot.send_message(
            settings.forum_chat_id, text, message_thread_id=settings.topic_games
        )
    except (TelegramBadRequest, TelegramRetryAfter):
        return None


async def safe_edit(
    bot: Bot,
    message_id: int | None,
    text: str,
    *,
    chat_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """edit_text не должен ронять хендлер/джобу: устаревшее сообщение
    (BadRequest) или флуд-контроль (RetryAfter, если ретраи сессии исчерпаны) —
    молча пропускаем. Состояние игры к этому моменту уже закоммичено."""
    if message_id is None:
        return
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id if chat_id is not None else settings.forum_chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
    except (TelegramBadRequest, TelegramRetryAfter):
        pass


async def safe_answer(callback: CallbackQuery, text: str | None = None) -> None:
    """Ack колбэка: при флуд-контроле ответ уходит позже 10–15 сек и API
    отвечает «query is too old» — это не ошибка игры, просто тост не показался."""
    try:
        await callback.answer(text)
    except (TelegramBadRequest, TelegramRetryAfter):
        pass


async def safe_react(bot: Bot, message: Message, emoji: str) -> None:
    """Эмодзи-реакция — best-effort украшение: любые ошибки (флуд, старое
    сообщение, выключенные реакции) молча глотаем, игру они не трогают."""
    try:
        from aiogram.types import ReactionTypeEmoji
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:  # noqa: BLE001 — реакции не должны ронять игру
        pass
