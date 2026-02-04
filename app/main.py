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
from aiogram.types import ErrorEvent, TelegramObject, Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import inspect, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.db import Base, engine, get_session
from app.handlers import admin, forms, games, help as help_handler, moderation, quiz
from app.models import MigrationFlag, QuizQuestion, UserStat
from app.services.topic_stats import bump_topic_stat
from app.services.quiz_loader import auto_load_quiz_questions
from app.services.games import (
    clear_game_command_messages,
    end_game,
    get_all_active_games,
    get_game_command_messages,
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
STOP_FLAG = Path("/app/data/.stopped")


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

            # Миграция user_stats
            if inspector.has_table("user_stats"):
                columns = {column["name"] for column in inspector.get_columns("user_stats")}
                if "display_name" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE user_stats ADD COLUMN display_name TEXT")
                    )

            # Миграция quiz_sessions
            if inspector.has_table("quiz_sessions"):
                columns = {column["name"] for column in inspector.get_columns("quiz_sessions")}
                if "used_question_ids" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE quiz_sessions ADD COLUMN used_question_ids TEXT")
                    )

        await conn.run_sync(_ensure_columns)


async def apply_v11_stats_reset(session: AsyncSession) -> None:
    """Единоразовый сброс статистики для v1.1."""
    flag = await session.get(MigrationFlag, "v11_stats_reset")
    if flag:
        return

    await session.execute(update(UserStat).values(coins=100, games_played=0, wins=0))
    session.add(MigrationFlag(key="v11_stats_reset"))
    await session.commit()
    logger.info("v1.1: статистика сброшена")


async def load_initial_quiz_questions(session: AsyncSession) -> None:
    """Загружает начальные вопросы для викторины."""
    flag = await session.get(MigrationFlag, "quiz_questions_initial_load")
    if flag:
        return

    from app.data.quiz_questions import INITIAL_QUESTIONS

    for question, answer in INITIAL_QUESTIONS:
        session.add(QuizQuestion(question=question, answer=answer))

    session.add(MigrationFlag(key="quiz_questions_initial_load"))
    await session.commit()
    logger.info(f"Загружено {len(INITIAL_QUESTIONS)} вопросов для викторины")


async def send_daily_summary(bot: Bot) -> None:
    if settings.topic_smoke is None:
        logger.info("Ежедневная сводка пропущена: topic_smoke не задан.")
        return
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
    if settings.topic_games is None:
        logger.info("Еженедельный рейтинг игр пропущен: topic_games не задан.")
        return
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
    if settings.topic_games is None:
        logger.info("Проверка таймаутов игр пропущена: topic_games не задан.")
        return
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


async def cleanup_blackjack_commands(bot: Bot) -> None:
    """Удаляет команды игры 21, отправленные в окно 22:00-00:00."""
    messages = []
    async for session in get_session():
        messages = await get_game_command_messages(session, settings.forum_chat_id)
        await clear_game_command_messages(session, settings.forum_chat_id)
        await session.commit()

    for record in messages:
        try:
            await bot.delete_message(record.chat_id, record.message_id)
        except Exception:
            pass


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
    scheduler.add_job(
        cleanup_blackjack_commands,
        "cron",
        hour=0,
        minute=1,
        args=[bot],
    )
    scheduler.add_job(
        quiz.announce_quiz_soon,
        "cron",
        hour=19,
        minute=55,
        args=[bot],
    )
    scheduler.add_job(
        quiz.start_quiz_auto,
        "cron",
        hour=20,
        minute=0,
        args=[bot],
    )
    scheduler.add_job(
        quiz.announce_quiz_soon,
        "cron",
        hour=20,
        minute=55,
        args=[bot],
    )
    scheduler.add_job(
        quiz.start_quiz_auto,
        "cron",
        hour=21,
        minute=0,
        args=[bot],
    )
    scheduler.add_job(
        auto_load_quiz_questions,
        "cron",
        day="*/3",
        hour=4,
        minute=0,
        args=[bot],
    )
    scheduler.start()
    return scheduler


async def on_startup(bot: Bot) -> None:
    await bot.get_me()  # заполняет bot.me с информацией о боте
    await init_db(engine)
    # Применяем миграции
    async for session in get_session():
        await apply_v11_stats_reset(session)
        await load_initial_quiz_questions(session)
    await heartbeat_job(bot)
    await bot.send_message(
        settings.admin_log_chat_id,
        f"🟢 Бот запущен\nВерсия: {settings.build_version}",
    )


async def error_handler(event: ErrorEvent) -> bool:
    """Глобальный обработчик ошибок — логирует и отправляет в админ-чат."""
    logger.exception(f"Ошибка: {event.exception}")

    error_text = (
        f"🔴 Ошибка в боте\n"
        f"Тип: {type(event.exception).__name__}\n"
        f"Сообщение: {event.exception}"
    )

    if event.update and event.update.message:
        msg = event.update.message
        error_text += f"\n\nКонтекст:\n"
        error_text += f"Chat: {msg.chat.id}\n"
        error_text += f"User: {msg.from_user.id if msg.from_user else 'N/A'}\n"
        error_text += f"Text: {(msg.text or '')[:100]}"

    try:
        await event.update.bot.send_message(settings.admin_log_chat_id, error_text)
    except Exception:
        pass  # Не падаем если не удалось отправить

    return True  # Ошибка обработана


async def main() -> None:
    # Проверка флага остановки — если бот был остановлен командой /shutdown_bot
    if STOP_FLAG.exists():
        logger.info("Бот остановлен. Удалите /app/data/.stopped для запуска.")
        return

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(LoggingMiddleware())
    dp.error.register(error_handler)

    # Порядок важен: упоминания должны ловиться до остальных обработчиков
    dp.include_router(help_handler.router)  # mention-help (catch-all, не блокирует)
    dp.include_router(admin.router)  # админ-команды
    dp.include_router(games.router)  # игры (команды /21, /score)
    dp.include_router(quiz.router)  # викторина (перед forms, т.к. есть catch-all)
    dp.include_router(forms.router)  # формы с FSM (перед модерацией!)
    dp.include_router(moderation.router)  # модерация (catch-all, пропускает FSM)
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
