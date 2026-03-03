import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import AiUsage, MessageLog, ModerationEvent, TopicStat
from app.services.db_maintenance import cleanup_old_data


async def _run_cleanup_old_data_check() -> dict[str, int]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime(2026, 3, 3, 12, 0, 0)
    old_log_time = now - timedelta(days=11)
    fresh_log_time = now - timedelta(days=5)

    async with session_factory() as session:
        session.add_all(
            [
                MessageLog(
                    chat_id=1,
                    topic_id=1,
                    user_id=1,
                    text="old",
                    severity=0,
                    created_at=old_log_time,
                ),
                MessageLog(
                    chat_id=1,
                    topic_id=1,
                    user_id=2,
                    text="new",
                    severity=0,
                    created_at=fresh_log_time,
                ),
                ModerationEvent(
                    chat_id=1,
                    user_id=1,
                    event_type="warn",
                    severity=1,
                    created_at=old_log_time,
                ),
                ModerationEvent(
                    chat_id=1,
                    user_id=1,
                    event_type="warn",
                    severity=1,
                    created_at=fresh_log_time,
                ),
                TopicStat(chat_id=1, topic_id=1, date_key="2026-01-01", messages_count=1),
                TopicStat(chat_id=1, topic_id=1, date_key="2026-03-01", messages_count=2),
                AiUsage(date_key="2026-01-01", chat_id=1, request_count=1, tokens_used=10),
                AiUsage(date_key="2026-03-01", chat_id=1, request_count=1, tokens_used=10),
            ]
        )
        await session.commit()

    from app.services import db_maintenance

    db_maintenance.settings.db_logs_retention_days = 10
    db_maintenance.settings.db_stats_retention_days = 20

    async with session_factory() as session:
        removed = await cleanup_old_data(session, now_utc=now)
        assert await session.get(AiUsage, {"date_key": "2026-03-01", "chat_id": 1}) is not None
        assert await session.get(AiUsage, {"date_key": "2026-01-01", "chat_id": 1}) is None

    await engine.dispose()
    return removed


def test_cleanup_old_data_removes_outdated_rows() -> None:
    removed = asyncio.run(_run_cleanup_old_data_check())
    assert removed == {
        "message_logs": 1,
        "moderation_events": 1,
        "topic_stats": 1,
        "ai_usage": 1,
    }
