"""Почему: главный модуль собирает роутеры, БД и планировщик в одном месте."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db import Base, engine, get_session
from app.handlers import admin, forms, games, help as help_handler, moderation, stats
from app.services.games import get_weekly_leaderboard
from app.services.health import get_health_state, update_heartbeat, update_notice
from app.services.topic_stats import get_daily_stats
from app.utils.time import now_tz

logging.basicConfig(level=logging.INFO)

HEARTBEAT_INTERVAL_MIN = 10
OFFLINE_THRESHOLD_MIN = 30


async def init_db(async_engine: AsyncEngine) -> None:
    if settings.database_url.startswith("sqlite+aiosqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite+aiosqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def send_daily_summary(bot: Bot) -> None:
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        stats_rows = await get_daily_stats(session, settings.forum_chat_id, date_key)
    if not stats_rows:
        return
    lines = ["Ежедневная сводка (с юмором):"]
    for row in stats_rows[:15]:
        lines.append(f"• Тема {row.topic_id}: сообщений {row.messages_count}.")
    text = "\n".join(lines)
    await bot.send_message(
        settings.forum_chat_id,
        text,
        message_thread_id=settings.topic_smoke,
    )


async def send_weekly_leaderboard(bot: Bot) -> None:
    async for session in get_session():
        top_coins, top_games = await get_weekly_leaderboard(session, settings.forum_chat_id)
    if not top_coins and not top_games:
        return
    lines = ["Еженедельный рейтинг игр:"]
    if top_coins:
        lines.append("Топ по монетам:")
        for stats_row in top_coins:
            lines.append(f"• {stats_row.user_id}: {stats_row.coins} монет")
    if top_games:
        lines.append("Топ по играм:")
        for stats_row in top_games:
            lines.append(f"• {stats_row.user_id}: {stats_row.games_played} игр")
    await bot.send_message(
        settings.forum_chat_id,
        "\n".join(lines),
        message_thread_id=settings.topic_games,
    )


async def heartbeat_job(bot: Bot) -> None:
    now = datetime.utcnow()
    async for session in get_session():
        state = await get_health_state(session)
        last_heartbeat = state.last_heartbeat_at
        if last_heartbeat and now - last_heartbeat > timedelta(minutes=OFFLINE_THRESHOLD_MIN):
            should_notify = state.last_notice_at is None or (now - state.last_notice_at > timedelta(days=1))
            if should_notify:
                await bot.send_message(
                    settings.admin_log_chat_id,
                    "Бот был недоступен. Сейчас снова онлайн.",
                )
                await update_notice(session, now)
        await update_heartbeat(session, now)
        await session.commit()


async def schedule_jobs(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(send_daily_summary, "cron", hour=21, minute=0, args=[bot])
    scheduler.add_job(send_weekly_leaderboard, "cron", day_of_week="sat", hour=21, minute=0, args=[bot])
    scheduler.add_job(heartbeat_job, "interval", minutes=HEARTBEAT_INTERVAL_MIN, args=[bot])
    scheduler.start()
    return scheduler


async def on_startup(bot: Bot) -> None:
    await init_db(engine)
    await heartbeat_job(bot)


async def main() -> None:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(help_handler.router)
    dp.include_router(admin.router)
    dp.include_router(forms.router)
    dp.include_router(games.router)
    dp.include_router(moderation.router)
    dp.include_router(stats.router)

    await on_startup(bot)
    scheduler = await schedule_jobs(bot)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
