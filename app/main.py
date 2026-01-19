"""Почему: главный модуль собирает роутеры, БД и планировщик в одном месте."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject, Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db import Base, engine, get_session
from app.handlers import admin, forms, games, help as help_handler, moderation
from app.services.topic_stats import bump_topic_stat
from app.services.games import (
    end_game,
    get_all_active_games,
    get_weekly_leaderboard,
    is_game_timed_out,
)
from app.services.health import get_health_state, update_heartbeat, update_notice
from app.services.topic_stats import get_daily_stats
from app.utils.time import now_tz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_MIN = 10


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update) and event.message:
            msg = event.message
            user = msg.from_user
            user_info = f"{user.full_name} (id={user.id})" if user else "unknown"
            text = msg.text or msg.caption or "[no text]"
            logger.info(
                f"IN: chat={msg.chat.id} topic={msg.message_thread_id} "
                f"user={user_info} text={text[:100]!r}"
            )
            # Сбор статистики по топикам (не блокирует хендлеры)
            if (
                msg.chat.id == settings.forum_chat_id
                and msg.message_thread_id is not None
                and msg.text
            ):
                date_key = now_tz().date().isoformat()
                async for session in get_session():
                    await bump_topic_stat(
                        session,
                        settings.forum_chat_id,
                        msg.message_thread_id,
                        date_key,
                        msg.text,
                    )
                    await session.commit()
        return await handler(event, data)
OFFLINE_THRESHOLD_MIN = 30


async def init_db(async_engine: AsyncEngine) -> None:
    if settings.database_url.startswith("sqlite+aiosqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite+aiosqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _ensure_columns(sync_conn: object) -> None:
            inspector = inspect(sync_conn)
            if not inspector.has_table("user_stats"):
                return
            columns = {column["name"] for column in inspector.get_columns("user_stats")}
            if "display_name" not in columns:
                sync_conn.execute(
                    text("ALTER TABLE user_stats ADD COLUMN display_name TEXT")
                )

        await conn.run_sync(_ensure_columns)


async def send_daily_summary(bot: Bot) -> None:
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        stats_rows = await get_daily_stats(session, settings.forum_chat_id, date_key)
    if not stats_rows:
        return
    lines = ["Ежедневная сводка (с юмором):"]
    for row in stats_rows[:15]:
        if row.last_message:
            cleaned = row.last_message.replace("\n", " ").strip()
            snippet = f" ({cleaned[:120]})"
        else:
            snippet = ""
        lines.append(f"• Тема {row.topic_id}: сообщений {row.messages_count}.{snippet}")
    text = "\n".join(lines)
    await bot.send_message(
        settings.forum_chat_id,
        text,
        message_thread_id=settings.topic_smoke,
    )


async def send_weekly_leaderboard(bot: Bot) -> None:
    async for session in get_session():
        top_coins, top_games = await get_weekly_leaderboard(
            session, settings.forum_chat_id
        )
    if not top_coins and not top_games:
        return
    lines = ["Еженедельный рейтинг игр:"]
    if top_coins:
        lines.append("Топ по монетам:")
        for stats_row in top_coins:
            name = stats_row.display_name or str(stats_row.user_id)
            lines.append(f"• {name}: {stats_row.coins} монет")
    if top_games:
        lines.append("Топ по играм:")
        for stats_row in top_games:
            name = stats_row.display_name or str(stats_row.user_id)
            lines.append(f"• {name}: {stats_row.games_played} игр")
    await bot.send_message(
        settings.forum_chat_id,
        "\n".join(lines),
        message_thread_id=settings.topic_games,
    )


async def heartbeat_job(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    async for session in get_session():
        state = await get_health_state(session)
        last_heartbeat = state.last_heartbeat_at
        if last_heartbeat and last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        if last_heartbeat and now - last_heartbeat > timedelta(
            minutes=OFFLINE_THRESHOLD_MIN
        ):
            should_notify = state.last_notice_at is None or (
                now - state.last_notice_at > timedelta(days=1)
            )
            if should_notify:
                await bot.send_message(
                    settings.admin_log_chat_id,
                    "Бот был недоступен. Сейчас снова онлайн.",
                )
                await update_notice(session, now)
        await update_heartbeat(session, now)
        await session.commit()


async def check_game_timeouts(bot: Bot) -> None:
    """Проверяет и отменяет просроченные игры (таймаут 10 минут)."""
    now = datetime.now(timezone.utc)
    async for session in get_session():
        games = await get_all_active_games(session)
        for user_id, chat_id, game_state in games:
            if is_game_timed_out(game_state, now):
                await end_game(session, user_id, chat_id)
                await session.commit()
                try:
                    await bot.send_message(
                        chat_id,
                        f"Время вышло! Игра отменена.",
                        message_thread_id=settings.topic_games,
                    )
                except Exception:
                    pass  # Не блокируем, если не удалось отправить


async def schedule_jobs(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    # Ежедневные сводки отключены
    # scheduler.add_job(send_daily_summary, "cron", hour=21, minute=0, args=[bot])
    scheduler.add_job(
        send_weekly_leaderboard,
        "cron",
        day_of_week="sat",
        hour=21,
        minute=0,
        args=[bot],
    )
    scheduler.add_job(
        heartbeat_job, "interval", minutes=HEARTBEAT_INTERVAL_MIN, args=[bot]
    )
    scheduler.add_job(
        check_game_timeouts, "interval", minutes=1, args=[bot]
    )
    scheduler.start()
    return scheduler


async def on_startup(bot: Bot) -> None:
    await bot.get_me()  # заполняет bot.me с информацией о боте
    await init_db(engine)
    await heartbeat_job(bot)


async def main() -> None:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(LoggingMiddleware())

    # Порядок важен! Catch-all роутеры должны быть в конце
    dp.include_router(admin.router)  # админ-команды
    dp.include_router(games.router)  # игры (команды /21, /score)
    dp.include_router(forms.router)  # формы с FSM (перед модерацией!)
    dp.include_router(moderation.router)  # модерация (catch-all, пропускает FSM)
    dp.include_router(help_handler.router)  # mention-help (catch-all) последним
    # stats.router убран — статистика через middleware

    await on_startup(bot)
    scheduler = await schedule_jobs(bot)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
