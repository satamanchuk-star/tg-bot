"""Почему: централизованный учёт лимитов ИИ упрощает контроль квот и диагностику."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiUsage
from app.utils.time import now_tz


@dataclass(slots=True)
class AiUsageStats:
    requests_used: int
    tokens_used: int


async def get_or_create_usage(
    session: AsyncSession,
    *,
    date_key: str,
    chat_id: int,
) -> AiUsage:
    usage = await session.get(AiUsage, {"date_key": date_key, "chat_id": chat_id})
    if usage is not None:
        return usage
    usage = AiUsage(date_key=date_key, chat_id=chat_id, request_count=0, tokens_used=0)
    session.add(usage)
    await session.flush()
    return usage


async def get_usage_stats(session: AsyncSession, *, date_key: str, chat_id: int) -> AiUsageStats:
    usage = await session.get(AiUsage, {"date_key": date_key, "chat_id": chat_id})
    if usage is None:
        return AiUsageStats(requests_used=0, tokens_used=0)
    return AiUsageStats(requests_used=usage.request_count, tokens_used=usage.tokens_used)


async def can_consume_ai(
    session: AsyncSession,
    *,
    date_key: str,
    chat_id: int,
    request_limit: int,
    token_limit: int,
) -> tuple[bool, str | None]:
    usage = await session.get(AiUsage, {"date_key": date_key, "chat_id": chat_id})
    if usage is None:
        return True, None
    if usage.request_count >= request_limit:
        return False, "достигнут лимит запросов"
    if token_limit > 0 and usage.tokens_used >= token_limit:
        return False, "достигнут лимит токенов"
    return True, None


async def add_usage(
    session: AsyncSession,
    *,
    date_key: str,
    chat_id: int,
    tokens_used: int,
) -> AiUsageStats:
    usage = await get_or_create_usage(session, date_key=date_key, chat_id=chat_id)
    usage.request_count += 1
    usage.tokens_used += max(0, tokens_used)
    usage.updated_at = datetime.utcnow()
    await session.commit()
    return AiUsageStats(requests_used=usage.request_count, tokens_used=usage.tokens_used)


async def reset_ai_usage(session: AsyncSession, *, chat_id: int | None = None) -> int:
    query = delete(AiUsage)
    if chat_id is not None:
        query = query.where(AiUsage.chat_id == chat_id)
    result = await session.execute(query)
    await session.commit()
    return int(result.rowcount or 0)


async def clear_old_usage(session: AsyncSession) -> int:
    today = now_tz().date().isoformat()
    result = await session.execute(delete(AiUsage).where(AiUsage.date_key != today))
    await session.commit()
    return int(result.rowcount or 0)


def next_reset_delta() -> timedelta:
    now = now_tz()
    tomorrow = (now + timedelta(days=1)).date()
    next_reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo)
    return next_reset - now
