"""Почему: сбор статистики отделен от модерации для гибкости."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.topic_stats import bump_topic_stat
from app.utils.time import now_tz

router = Router()


@router.message()
async def capture_stats(message: Message) -> None:
    if message.chat.id != settings.forum_chat_id:
        return
    if message.message_thread_id is None:
        return
    if message.text is None:
        return
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        await bump_topic_stat(
            session,
            settings.forum_chat_id,
            message.message_thread_id,
            date_key,
            message.text,
        )
        await session.commit()
