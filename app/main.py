"""Почему: главный модуль собирает роутеры, БД и планировщик в одном месте."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.utils.token import TokenValidationError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChatAdministrators,
    ErrorEvent,
    TelegramObject,
    Update,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import Integer, inspect, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.db import Base, engine, get_session
from app.handlers import (
    admin,
    economy as economy_handler,
    forms,
    games,
    help as help_handler,
    moderation,
    personalization as personalization_handler,
    roulette,
    shop,
    suggest,
    text_publish,
    welcome,
)
from app.models import MigrationFlag, UserStat
from app.services.topic_stats import bump_topic_stat
from app.services.games import (
    clear_game_command_messages,
    end_game,
    get_all_active_games,
    get_game_command_messages,
    get_weekly_leaderboard,
    is_game_timed_out,
)
from app.services.health import get_health_state, update_heartbeat, update_notice
from app.services.db_maintenance import cleanup_old_data, optimize_sqlite
from app.utils.time import now_tz
from app.services.ai_module import clear_assistant_cache, close_ai_client, get_ai_client, get_and_clear_response_log, set_ai_admin_notifier
from app.services.proxy import ProxyManager
from app.services.daily_summary import build_ai_summary_context, build_daily_summary, build_response_report, render_daily_summary
from app.services.daily_messages import send_morning_greeting
from app.services.personalization import send_weekly_nudges
from app.services.sheets import sync_places_from_sheet
from app.services.resident_kb import load_resident_kb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """Добавляет RotatingFileHandler — пишем лог в файл рядом с БД."""
    log_dir = settings.data_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot.log"
    handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 МБ на файл
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    logger.info("Файловый лог: %s (ротация 10 МБ × 5 файлов)", log_file)


_setup_file_logging()

HEARTBEAT_INTERVAL_MIN = 10
STOP_FLAG = settings.data_dir / ".stopped"


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
                    try:
                        await bump_topic_stat(
                            session,
                            settings.forum_chat_id,
                            msg.message_thread_id,
                            date_key,
                            msg.text,
                        )
                        await session.commit()
                    except OperationalError as exc:
                        logger.warning("Не удалось обновить статистику тем: %s", exc)
                        await session.rollback()
        return await handler(event, data)


MAX_RETRIES_ON_FLOOD = 3
MAX_RETRIES_ON_NETWORK = 3
NETWORK_RETRY_BACKOFF = (1.0, 2.0, 4.0)

# Глобальный менеджер прокси; None если прокси не включены
_proxy_manager: ProxyManager | None = None


class RetryOnFloodSession(AiohttpSession):
    """Повторяет запросы при флуд-контроле и сетевых сбоях; ротирует прокси при необходимости.

    Если передан proxy_manager — при каждом сетевом сбое переключается на
    следующий прокси из пула перед повтором запроса.
    """

    def __init__(self, proxy_manager: ProxyManager | None = None, **kwargs):
        proxy = proxy_manager.get_current() if proxy_manager else None
        try:
            super().__init__(proxy=proxy, **kwargs)
        except RuntimeError as exc:
            # aiogram требует aiohttp-socks для любого прокси; если пакет не установлен
            # или прокси битый — запускаемся без прокси, чтобы не уронить весь бот.
            logger.warning("Не удалось инициализировать прокси (%s), запускаюсь без прокси", exc)
            super().__init__(proxy=None, **kwargs)
            proxy_manager = None
        self._proxy_manager = proxy_manager

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        flood_attempts = 0
        network_attempts = 0
        while True:
            try:
                return await super().make_request(bot, method, timeout=timeout)
            except TelegramRetryAfter as e:
                flood_attempts += 1
                if flood_attempts >= MAX_RETRIES_ON_FLOOD:
                    raise
                logger.warning(
                    "Flood control, жду %s сек (попытка %d/%d)",
                    e.retry_after, flood_attempts, MAX_RETRIES_ON_FLOOD,
                )
                await asyncio.sleep(e.retry_after)
            except TelegramNetworkError as e:
                network_attempts += 1
                if self._proxy_manager:
                    new_proxy = self._proxy_manager.rotate()
                    try:
                        # Закрываем текущую сессию — следующий вызов super().make_request
                        # создаст новую с обновлённым proxy (AiohttpSession ленив).
                        await self.close()
                        self.proxy = new_proxy
                    except Exception as proxy_exc:
                        logger.warning("Не удалось сменить прокси (%s), отключаю ротацию", proxy_exc)
                        self._proxy_manager = None
                if network_attempts >= MAX_RETRIES_ON_NETWORK:
                    logger.warning(
                        "Сетевой сбой Telegram после %d попыток: %s",
                        network_attempts, e,
                    )
                    raise
                delay = NETWORK_RETRY_BACKOFF[
                    min(network_attempts - 1, len(NETWORK_RETRY_BACKOFF) - 1)
                ]
                logger.warning(
                    "Сетевой сбой Telegram (%s), ретрай через %.1f сек (попытка %d/%d)",
                    e, delay, network_attempts, MAX_RETRIES_ON_NETWORK,
                )
                await asyncio.sleep(delay)


OFFLINE_THRESHOLD_MIN = 30
STARTUP_AI_PROBE_TIMEOUT_SECONDS = 5
TG_PROBE_TIMEOUT_SECONDS = 10  # максимум ждём ответа при старте


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
                columns = {
                    column["name"] for column in inspector.get_columns("user_stats")
                }
                if "display_name" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE user_stats ADD COLUMN display_name TEXT")
                    )

            # Миграция moderation_events
            if inspector.has_table("moderation_events"):
                columns = {
                    column["name"]
                    for column in inspector.get_columns("moderation_events")
                }
                if "message_id" not in columns:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE moderation_events ADD COLUMN message_id INTEGER"
                        )
                    )
                if "reason" not in columns:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE moderation_events ADD COLUMN reason TEXT"
                        )
                    )
                if "confidence" not in columns:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE moderation_events ADD COLUMN confidence REAL"
                        )
                    )

            # Миграция rag_messages
            if inspector.has_table("rag_messages"):
                columns = {
                    column["name"] for column in inspector.get_columns("rag_messages")
                }
                if "rag_category" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE rag_messages ADD COLUMN rag_category VARCHAR(50)")
                    )
                if "rag_semantic_key" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE rag_messages ADD COLUMN rag_semantic_key VARCHAR(120)")
                    )
                if "rag_canonical_text" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE rag_messages ADD COLUMN rag_canonical_text TEXT")
                    )
                if "expires_at" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE rag_messages ADD COLUMN expires_at DATETIME")
                    )
                if "is_admin" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE rag_messages ADD COLUMN is_admin BOOLEAN DEFAULT 0 NOT NULL")
                    )

            # Миграция moderation_training: original_message_id
            if inspector.has_table("moderation_training"):
                columns = {
                    column["name"] for column in inspector.get_columns("moderation_training")
                }
                if "original_message_id" not in columns:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE moderation_training ADD COLUMN original_message_id INTEGER"
                        )
                    )

            # Миграция message_logs: sentiment
            if inspector.has_table("message_logs"):
                columns = {
                    column["name"] for column in inspector.get_columns("message_logs")
                }
                if "sentiment" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE message_logs ADD COLUMN sentiment VARCHAR(20)")
                    )

            if inspector.has_table("places"):
                sync_conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_places_name_address_category_idx "
                        "ON places(name, address, category)"
                    )
                )

            if inspector.has_table("resident_services"):
                duplicate_rows = sync_conn.execute(
                    text(
                        "SELECT chat_id, source_message_id, MAX(id) AS keep_id "
                        "FROM resident_services "
                        "WHERE source_message_id IS NOT NULL "
                        "GROUP BY chat_id, source_message_id "
                        "HAVING COUNT(*) > 1"
                    )
                ).fetchall()
                for duplicate in duplicate_rows:
                    sync_conn.execute(
                        text(
                            "UPDATE resident_services "
                            "SET source_message_id = NULL "
                            "WHERE chat_id = :chat_id "
                            "AND source_message_id = :source_message_id "
                            "AND id <> :keep_id"
                        ),
                        {
                            "chat_id": duplicate.chat_id,
                            "source_message_id": duplicate.source_message_id,
                            "keep_id": duplicate.keep_id,
                        },
                    )
                sync_conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_resident_services_chat_source_message_idx "
                        "ON resident_services(chat_id, source_message_id)"
                    )
                )

            # Миграция resident_profiles (создаётся через create_all,
            # но проверяем на всякий случай)
            if inspector.has_table("resident_profiles"):
                columns = {
                    column["name"]
                    for column in inspector.get_columns("resident_profiles")
                }
                if "last_nudge_at" not in columns:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE resident_profiles ADD COLUMN last_nudge_at DATETIME"
                        )
                    )


        await conn.run_sync(_ensure_columns)


async def validate_db(async_engine: AsyncEngine) -> None:
    """Проверяет целостность БД и логирует ключевые параметры при старте."""
    logger.info("=== Проверка БД ===")
    async with async_engine.begin() as conn:
        if settings.database_url.startswith("sqlite+"):
            # Целостность файла
            rows = (await conn.execute(text("PRAGMA integrity_check"))).fetchall()
            status = rows[0][0] if rows else "unknown"
            if status == "ok":
                logger.info("БД integrity_check: ok")
            else:
                details = "; ".join(r[0] for r in rows)
                logger.error("БД integrity_check ПРОВАЛЕНА: %s", details)

            # Режим журнала (ожидаем WAL)
            jm = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            logger.info("БД journal_mode: %s", jm)

            # Примерный размер
            page_count = (await conn.execute(text("PRAGMA page_count"))).scalar() or 0
            page_size = (await conn.execute(text("PRAGMA page_size"))).scalar() or 0
            db_size_kb = page_count * page_size // 1024
            logger.info("БД размер: ~%d КБ (%d страниц)", db_size_kb, page_count)

        # Список таблиц
        def _list_tables(sync_conn: object) -> list[str]:
            return inspect(sync_conn).get_table_names()

        tables = await conn.run_sync(_list_tables)
        logger.info("БД таблицы (%d): %s", len(tables), ", ".join(sorted(tables)))

    logger.info("=== Проверка БД завершена ===")


async def apply_v11_stats_reset(session: AsyncSession) -> None:
    """Единоразовый сброс статистики для v1.1."""
    flag = await session.get(MigrationFlag, "v11_stats_reset")
    if flag:
        return

    await session.execute(update(UserStat).values(coins=100, games_played=0, wins=0))
    session.add(MigrationFlag(key="v11_stats_reset"))
    await session.commit()
    logger.info("v1.1: статистика сброшена")


async def drop_orphaned_tables(session: AsyncSession) -> None:
    """Удаляет осиротевшие таблицы quiz_* и lottery_tickets."""
    flag = await session.get(MigrationFlag, "drop_orphaned_quiz_lottery")
    if flag:
        return

    orphaned = [
        "lottery_tickets",
        "quiz_daily_limits",
        "quiz_questions",
        "quiz_sessions",
        "quiz_used_questions",
        "quiz_user_stats",
    ]
    for table_name in orphaned:
        await session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    session.add(MigrationFlag(key="drop_orphaned_quiz_lottery"))
    await session.commit()
    logger.info("Удалены осиротевшие таблицы: %s", ", ".join(orphaned))


async def send_daily_summary(bot: Bot) -> None:
    if not settings.ai_feature_daily_summary:
        logger.info("Ежедневная сводка пропущена: ai_feature_daily_summary=false.")
        return
    target_chat_id = settings.forum_chat_id
    target_thread_id = settings.ai_summary_topic_id
    if target_thread_id is None:
        target_chat_id = settings.admin_log_chat_id
    async for session in get_session():
        summary = await build_daily_summary(session, settings.forum_chat_id)
        break
    else:
        logger.error("Не удалось получить сессию БД для ежедневной сводки.")
        return
    stats_text = render_daily_summary(summary)

    # Генерируем AI-сводку, если доступен провайдер
    ai_summary_text = ""
    if summary.messages > 0:
        try:
            from app.services.ai_module import get_ai_client
            ai_client = get_ai_client()
            ai_context = build_ai_summary_context(summary)
            ai_summary = await ai_client.generate_daily_summary(
                ai_context, chat_id=settings.forum_chat_id,
            )
            if ai_summary and ai_summary.strip():
                ai_summary_text = f"\n\n🤖 Резюме от ИИ:\n{ai_summary.strip()}"
        except Exception:
            logger.warning("Не удалось сгенерировать AI-сводку, отправляем только статистику.")

    full_text = stats_text + ai_summary_text

    for attempt in range(1, 4):
        try:
            await bot.send_message(
                target_chat_id,
                full_text,
                message_thread_id=target_thread_id,
            )
            return
        except Exception:
            if attempt >= 3:
                await bot.send_message(
                    settings.admin_log_chat_id,
                    "Не удалось отправить ежедневную сводку после 3 попыток.",
                )
                return
            await asyncio.sleep(2)



async def send_daily_response_report(bot: Bot) -> None:
    """Отправляет в чат логов ежедневный отчёт по логике ответов бота."""
    response_log = get_and_clear_response_log()
    report_text = build_response_report(response_log)
    for attempt in range(1, 4):
        try:
            await bot.send_message(settings.admin_log_chat_id, report_text)
            return
        except Exception:
            if attempt >= 3:
                logger.error("Не удалось отправить отчёт по ответам бота после 3 попыток.")
                return
            await asyncio.sleep(2)


async def send_weekly_leaderboard(bot: Bot) -> None:
    if settings.topic_games is None:
        logger.info("Еженедельный рейтинг игр пропущен: topic_games не задан.")
        return
    async for session in get_session():
        top_coins, top_games = await get_weekly_leaderboard(
            session, settings.forum_chat_id
        )
        break
    else:
        logger.error("Не удалось получить сессию БД для еженедельного рейтинга.")
        return
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
            last_notice = state.last_notice_at
            if last_notice and last_notice.tzinfo is None:
                last_notice = last_notice.replace(tzinfo=timezone.utc)
            should_notify = last_notice is None or (
                now - last_notice > timedelta(days=1)
            )
            if should_notify:
                try:
                    await bot.send_message(
                        settings.admin_log_chat_id,
                        "Бот был недоступен. Сейчас снова онлайн.",
                    )
                except (TelegramNetworkError, TelegramAPIError) as exc:
                    logger.warning(
                        "Не удалось отправить heartbeat-уведомление в Telegram: %s",
                        exc,
                    )
                else:
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
                        "Время вышло! Игра отменена.",
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


async def cleanup_database() -> None:
    """Удаляет старые техданные и уменьшает размер SQLite-файла."""
    stats: dict[str, int] | None = None
    async for session in get_session():
        stats = await cleanup_old_data(session)
        await optimize_sqlite(session)
    if stats is None:
        return
    logger.info(
        "Очистка БД завершена: message_logs=%s moderation_events=%s topic_stats=%s ai_usage=%s rag_expired=%s chat_history=%s ai_feedback=%s faq=%s",
        stats["message_logs"],
        stats["moderation_events"],
        stats["topic_stats"],
        stats["ai_usage"],
        stats.get("rag_expired", 0),
        stats.get("chat_history", 0),
        stats.get("ai_feedback", 0),
        stats.get("frequent_questions", 0),
    )


async def _sync_places_from_sheets() -> None:
    """Загружает справочник инфраструктуры из Google Sheets в БД (не блокирует старт)."""
    if not settings.google_service_account_file:
        logger.info("Импорт инфраструктуры пропущен: GOOGLE_SERVICE_ACCOUNT_FILE не задан.")
        return
    try:
        from scripts.import_places_from_google_sheets import run_import
        stats = await run_import(dry_run=False)
        logger.info(
            "Импорт инфраструктуры: created=%s updated=%s skipped=%s errors=%s",
            stats.created, stats.updated, stats.skipped, stats.errors,
        )
    except Exception:
        logger.exception("Ошибка импорта инфраструктуры из Google Sheets.")


def _run_background_task(coro: Awaitable[None], *, name: str) -> None:
    """Запускает фоновую задачу и логирует исключения без падения процесса."""
    task = asyncio.create_task(coro, name=name)

    def _on_done(done_task: asyncio.Task[None]) -> None:
        try:
            done_task.result()
        except Exception:
            logger.exception("Фоновая задача %s завершилась с ошибкой.", name)

    task.add_done_callback(_on_done)


def _cleanup_flood_tracker() -> None:
    """Периодическая очистка FloodTracker от устаревших записей."""
    from app.handlers.moderation import FLOOD_TRACKER
    removed = FLOOD_TRACKER.cleanup()
    if removed:
        logger.debug("FloodTracker cleanup: удалено %d записей", removed)


async def schedule_jobs(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    if _proxy_manager is not None:
        scheduler.add_job(
            _proxy_manager.refresh_and_find,
            "interval",
            minutes=settings.proxy_refresh_interval_min,
        )
    scheduler.add_job(
        send_daily_summary,
        "cron",
        hour=settings.ai_summary_hour,
        minute=settings.ai_summary_minute,
        args=[bot],
    )
    scheduler.add_job(
        send_daily_response_report,
        "cron",
        hour=settings.ai_summary_hour,
        minute=(settings.ai_summary_minute + 2) % 60,
        args=[bot],
    )
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
    scheduler.add_job(check_game_timeouts, "interval", minutes=1, args=[bot])
    scheduler.add_job(_cleanup_flood_tracker, "interval", minutes=10)
    scheduler.add_job(
        cleanup_blackjack_commands,
        "cron",
        hour=0,
        minute=1,
        args=[bot],
    )
    scheduler.add_job(
        cleanup_database,
        "cron",
        hour=4,
        minute=20,
    )
    scheduler.add_job(
        _sync_places_from_sheets,
        "cron",
        hour="0,6,12,18",
        minute=30,
    )
    # Рулетка: анонс → правила → запуск первого раунда
    scheduler.add_job(
        roulette.announce_roulette_soon,
        "cron",
        hour=20,
        minute=55,
        args=[bot],
    )
    scheduler.add_job(
        roulette.announce_roulette_rules,
        "cron",
        hour=20,
        minute=59,
        args=[bot],
    )
    scheduler.add_job(
        roulette.start_roulette_round,
        "cron",
        hour=21,
        minute=0,
        args=[bot],
    )
    # Блэкджек: правила за минуту до старта
    scheduler.add_job(
        games.announce_blackjack_rules,
        "cron",
        hour=21,
        minute=59,
        args=[bot],
    )
    # Недельный дайджест по воскресеньям (20:00)
    scheduler.add_job(
        send_weekly_digest,
        "cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        args=[bot],
    )
    # Еженедельные персональные DM-нажъмы (по фактам из ResidentProfile).
    # Off-by-default через ai_feature_weekly_nudge — внутри функции стоит ранний return.
    # Вторник 11:00 — середина рабочей недели, не путается с проактивными
    # утренними/вечерними коммуникациями и понедельничным weekly update.
    scheduler.add_job(
        send_weekly_nudges,
        "cron",
        day_of_week="tue",
        hour=11,
        minute=0,
        args=[bot],
    )
    # Утреннее приветствие с погодой и праздниками (8:00 каждый день)
    if settings.ai_daily_greeting:
        scheduler.add_job(
            send_morning_greeting,
            "cron",
            hour=8,
            minute=0,
            args=[bot],
        )
    scheduler.start()
    return scheduler


async def on_startup_critical() -> None:
    """Блокирующая часть инициализации: только то, без чего нельзя стартовать polling.

    Всё тяжёлое (TG probe, set_commands, seed, resident_kb, AI probe, startup-notice)
    вынесено в ``on_startup_warmup`` и запускается фоном после старта polling.
    """
    import time as _time
    _step_t = _time.monotonic()
    try:
        await init_db(engine)
    except Exception:
        logger.exception(
            "Критическая ошибка: не удалось инициализировать БД. "
            "Проверьте DATABASE_URL и права на директорию данных."
        )
        raise
    logger.info("⏱ init_db: %.2fs", _time.monotonic() - _step_t)

    _step_t = _time.monotonic()
    try:
        async for session in get_session():
            await apply_v11_stats_reset(session)
            await drop_orphaned_tables(session)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось выполнить миграции (некритично, продолжаем).")
    logger.info("⏱ migrations: %.2fs", _time.monotonic() - _step_t)


async def on_startup_warmup(bot: Bot) -> None:
    """Фоновый прогрев: probes, set_commands, seed, resident_kb, AI probe, уведомление.

    Запускается параллельно со стартом polling — пользователи начинают получать ответы
    сразу, а долгие сетевые probes и seed'ы происходят в фоне.
    """
    async def _admin_notifier(text: str) -> None:
        await bot.send_message(settings.admin_log_chat_id, f"⚠️ {text}")

    set_ai_admin_notifier(_admin_notifier)
    telegram_available = False
    tg_probe_note = ""
    import time as _time
    _t_startup = _time.monotonic()

    # ── Telegram API probe ────────────────────────────────────────────────────
    _step_t = _time.monotonic()
    for attempt in range(1, 4):
        try:
            _t0 = _time.monotonic()
            # wait_for ограничивает время ожидания, иначе RetryOnFloodSession
            # может ждать десятки секунд внутри при flood-control от Telegram.
            bot_me = await asyncio.wait_for(
                bot.get_me(), timeout=TG_PROBE_TIMEOUT_SECONDS
            )
            _tg_latency_ms = int((_time.monotonic() - _t0) * 1000)
            telegram_available = True
            tg_probe_note = f"Telegram API: ✅ доступен ({_tg_latency_ms} ms)"
            logger.info("Telegram API probe: ok=True latency_ms=%d", _tg_latency_ms)
            # Прогреваем кэш профиля в help-роутере, чтобы первое упоминание
            # не тратило лишний HTTP-round-trip на bot.get_me().
            try:
                help_handler.prewarm_bot_profile(bot_me)
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось прогреть кэш профиля бота в help-роутере.")
            break
        except asyncio.TimeoutError:
            if attempt >= 3:
                tg_probe_note = (
                    f"Telegram API: ⚠️ медленно (>{TG_PROBE_TIMEOUT_SECONDS} с)"
                )
                logger.warning(
                    "Telegram API probe: таймаут %d с после 3 попыток.",
                    TG_PROBE_TIMEOUT_SECONDS,
                )
                break
            logger.warning(
                "Telegram API probe: таймаут %d с, попытка %d/3. Повтор через 5 секунд.",
                TG_PROBE_TIMEOUT_SECONDS,
                attempt,
            )
            await asyncio.sleep(5)
        except TelegramNetworkError:
            if attempt >= 3:
                tg_probe_note = "Telegram API: ❌ недоступен (нет соединения)"
                logger.warning(
                    "Нет соединения с Telegram API после 3 попыток. "
                    "Продолжаем запуск и передаём переподключение polling-циклу."
                )
                break
            logger.warning(
                "Нет соединения с Telegram API, попытка %s/3. Повтор через 5 секунд.",
                attempt,
            )
            await asyncio.sleep(5)
        except TelegramAPIError as exc:
            tg_probe_note = f"Telegram API: ❌ ошибка — {exc}"
            logger.error(
                "Не удалось получить данные бота из Telegram API: %s "
                "(token=%s..., len=%d). Проверьте BOT_TOKEN и права доступа.",
                exc,
                settings.bot_token[:10],
                len(settings.bot_token),
            )
            break
    logger.info("⏱ tg_probe: %.2fs", _time.monotonic() - _step_t)

    # ── БД: проверка целостности и очистка — в фон, не блокируем старт ────────
    async def _bg_validate_and_cleanup() -> None:
        try:
            await validate_db(engine)
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка при проверке БД (некритично).")
        await cleanup_database()

    _run_background_task(_bg_validate_and_cleanup(), name="startup_validate_cleanup")

    # ── Heartbeat — в фон, не блокируем старт ───────────────────────────────
    _run_background_task(heartbeat_job(bot), name="startup_heartbeat")

    # ── Команды бота в меню Telegram — оба вызова параллельно ────────────────
    _step_t = _time.monotonic()
    if telegram_available:
        async def _set_public_commands() -> None:
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="help", description="Справка и навигация по форуму"),
                        BotCommand(command="rules", description="Правила нашего сообщества"),
                        BotCommand(command="ai", description="Задать вопрос Жаботу"),
                        BotCommand(command="21", description="Играть в блэкджек"),
                        BotCommand(command="21top", description="Топ игроков недели"),
                        BotCommand(command="roulette", description="Играть в рулетку"),
                        BotCommand(command="bet", description="Сделать ставку в рулетке"),
                        BotCommand(command="score", description="Мои монеты и статистика"),
                        BotCommand(command="предложить", description="Предложить место в инфраструктуре ЖК"),
                    ],
                )
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось зарегистрировать публичные команды в Telegram меню.")

        async def _set_admin_commands() -> None:
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="admin", description="📋 Меню всех админ-команд"),
                        BotCommand(command="mute", description="Замьютить пользователя (реплай)"),
                        BotCommand(command="unmute", description="Снять мут (реплай)"),
                        BotCommand(command="ban", description="Забанить пользователя (реплай)"),
                        BotCommand(command="unban", description="Снять бан (реплай)"),
                        BotCommand(command="strike", description="Выдать страйк (реплай)"),
                        BotCommand(command="addcoins", description="Начислить монеты (реплай)"),
                        BotCommand(command="ai_status", description="Статус и диагностика ИИ"),
                        BotCommand(command="ai_probe", description="AI probe — проверка 3 слоёв"),
                        BotCommand(command="ai_on", description="Включить AI runtime"),
                        BotCommand(command="ai_off", description="Выключить AI runtime"),
                        BotCommand(command="training_on", description="Включить режим обучения"),
                        BotCommand(command="training_off", description="Выключить режим обучения"),
                        BotCommand(command="reload_profanity", description="Перечитать мат-словари"),
                        BotCommand(command="reset_routing_state", description="Сбросить ожидания роутинга"),
                        BotCommand(command="reset_stats", description="Сбросить статистику"),
                        BotCommand(command="form", description="Форма для шлагбаума"),
                        BotCommand(command="text", description="Текст от лица бота"),
                        BotCommand(command="rag_bot", description="Добавить запись в RAG базу"),
                        BotCommand(command="rag_sync", description="Систематизировать RAG базу"),
                        BotCommand(command="restart_jobs", description="Перезапуск зависших задач"),
                        BotCommand(command="shutdown_bot", description="⚠️ Остановить бота"),
                    ],
                    scope=BotCommandScopeChatAdministrators(
                        chat_id=settings.forum_chat_id,
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось зарегистрировать админ-команды в Telegram меню.")

        await asyncio.gather(_set_public_commands(), _set_admin_commands())
    logger.info("⏱ set_commands: %.2fs", _time.monotonic() - _step_t)

    # ── Кэши и база знаний ───────────────────────────────────────────────────
    _step_t = _time.monotonic()
    cleared = clear_assistant_cache()
    if cleared:
        logger.info("Сброшен AI-кэш: %d записей.", cleared)
    load_resident_kb.cache_clear()  # Сброс lru_cache, чтобы подхватить актуальный файл
    try:
        load_resident_kb()
    except Exception:
        logger.exception("Не удалось загрузить базу знаний жителей (resident_kb.json).")
    logger.info("⏱ resident_kb: %.2fs", _time.monotonic() - _step_t)

    # ── Seed инфраструктуры из JSON ──────────────────────────────────────────
    _step_t = _time.monotonic()
    try:
        from scripts.seed_places import purge_old_places, seed_places
        async for session in get_session():
            purged = await purge_old_places(session)
            seeded = await seed_places(session)
            if purged or seeded:
                await session.commit()
                logger.info("Инфраструктура: удалено %s, добавлено %s объектов.", purged, seeded)
    except Exception:
        logger.exception("Ошибка seed инфраструктуры.")
    logger.info("⏱ seed_places: %.2fs", _time.monotonic() - _step_t)

    # ── Google Sheets — в фон ────────────────────────────────────────────────
    _run_background_task(_sync_places_from_sheets(), name="startup_sync_places")

    # ── Рулетка ──────────────────────────────────────────────────────────────
    _step_t = _time.monotonic()
    try:
        await roulette.resume_roulette_if_needed(bot)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось возобновить рулетку при старте (некритично, продолжаем).")
    logger.info("⏱ roulette_resume: %.2fs", _time.monotonic() - _step_t)

    # ── AI клиент + probe ────────────────────────────────────────────────────
    _step_t = _time.monotonic()
    get_ai_client()
    ai_probe_note = ""
    if settings.ai_enabled and settings.ai_key:
        source_note = " (по умолчанию, AI_MODEL не задан)" if settings.ai_model_is_default else ""
        ai_mode = f"AI: OpenRouter ({settings.ai_model}){source_note}"
        try:
            probe = await asyncio.wait_for(
                get_ai_client().probe(),
                timeout=STARTUP_AI_PROBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            ai_probe_note = (
                "API: ⚠️ проверка превышает лимит старта "
                f"({STARTUP_AI_PROBE_TIMEOUT_SECONDS} c)"
            )
            logger.warning("AI probe timeout on startup after %s seconds.", STARTUP_AI_PROBE_TIMEOUT_SECONDS)
        else:
            if probe.ok:
                ai_probe_note = f"API: ✅ доступен ({probe.latency_ms} ms)"
            else:
                ai_probe_note = f"API: ❌ недоступен — {probe.details}"
            logger.info("AI probe: ok=%s details=%s latency_ms=%s", probe.ok, probe.details, probe.latency_ms)
    elif not settings.ai_enabled:
        ai_mode = "AI: отключен (AI_ENABLED=false)"
    else:
        ai_mode = "AI: отключен (AI_KEY не задан)"
    logger.info("AI модуль: %s", ai_mode)
    logger.info("⏱ ai_probe: %.2fs", _time.monotonic() - _step_t)

    logger.info("⏱ ИТОГО on_startup: %.2fs", _time.monotonic() - _t_startup)

    # ── Сообщение о запуске ──────────────────────────────────────────────────
    if telegram_available:
        lines = [f"🟢 Бот запущен", f"Версия: {settings.build_version}", ai_mode]
        if tg_probe_note:
            lines.append(tg_probe_note)
        if ai_probe_note:
            lines.append(ai_probe_note)
        await bot.send_message(settings.admin_log_chat_id, "\n".join(lines))


_ERROR_NOTIFY_WINDOW_SECONDS = 300  # окно дедупликации (5 минут)
_ERROR_NOTIFY_MAX = 100
_error_notify_last: dict[str, float] = {}


def _should_notify_error(signature: str) -> bool:
    """Возвращает True, если такую ошибку ещё не показывали в окне дедупликации."""
    import time as _t
    now = _t.monotonic()
    # Очищаем устаревшие записи, чтобы dict не рос бесконечно
    if len(_error_notify_last) >= _ERROR_NOTIFY_MAX:
        stale = [
            k for k, v in _error_notify_last.items()
            if now - v > _ERROR_NOTIFY_WINDOW_SECONDS
        ]
        for key in stale:
            _error_notify_last.pop(key, None)
    last = _error_notify_last.get(signature)
    if last is not None and now - last < _ERROR_NOTIFY_WINDOW_SECONDS:
        return False
    _error_notify_last[signature] = now
    return True


async def error_handler(event: ErrorEvent) -> bool:
    """Глобальный обработчик ошибок — логирует и отправляет в админ-чат."""
    exc = event.exception
    logger.exception(f"Ошибка: {exc}")

    # Сетевые сбои Telegram обычно транзиентные (таймаут, обрыв соединения).
    # Мы их уже ретраем в RetryOnFloodSession, поэтому в админ-чат отправляем
    # только при сериях повторных сбоев — один раз на окно дедупликации.
    is_transient_network = isinstance(exc, TelegramNetworkError)
    signature = f"{type(exc).__name__}:{str(exc)[:120]}"
    if is_transient_network and not _should_notify_error(signature):
        return True

    error_text = (
        f"🔴 Ошибка в боте\n"
        f"Тип: {type(exc).__name__}\n"
        f"Сообщение: {exc}"
    )

    if event.update and event.update.message:
        msg = event.update.message
        error_text += "\n\nКонтекст:\n"
        error_text += f"Chat: {msg.chat.id}\n"
        error_text += f"User: {msg.from_user.id if msg.from_user else 'N/A'}\n"
        preview = (msg.text or msg.caption or "").strip()
        if not preview and msg.content_type:
            preview = f"[{msg.content_type}]"
        error_text += f"Text: {preview[:100]}"

    try:
        await event.update.bot.send_message(settings.admin_log_chat_id, error_text)
    except Exception:
        pass  # Не падаем если не удалось отправить

    return True  # Ошибка обработана


async def main() -> None:
    logger.info(
        "Запуск бота: version=%s data_dir=%s db=%s ai_enabled=%s ai_key_set=%s",
        settings.build_version,
        settings.data_dir,
        settings.database_url.split("///")[-1] if "///" in settings.database_url else "???",
        settings.ai_enabled,
        bool(settings.ai_key),
    )

    # Проверка флага остановки — если бот был остановлен командой /shutdown_bot
    if STOP_FLAG.exists():
        logger.error(
            "Бот остановлен флагом %s (создан командой /shutdown_bot). "
            "Удалите файл для запуска: rm %s",
            STOP_FLAG,
            STOP_FLAG,
        )
        raise SystemExit(1)

    # Инициализация прокси (до создания бота, чтобы сессия сразу получила адрес)
    global _proxy_manager
    if settings.proxy_enabled or settings.proxy_manual:
        _proxy_manager = ProxyManager(
            working_pool_size=settings.proxy_working_pool_size,
            test_limit=settings.proxy_test_limit,
            state_path=Path(settings.proxy_state_path),
            manual_proxy=settings.proxy_manual,
        )
        if settings.proxy_manual:
            logger.info("Прокси: ручной адрес задан, автоподбор пропущен")
            ok = await _proxy_manager.validate_working_pool()
            if not ok:
                logger.warning(
                    "Ручной прокси не отвечает, всё равно пробуем через него "
                    "(Telegram может быть временно недоступен для прокси)"
                )
        else:
            logger.info("Прокси включены, ищу рабочие…")
            # Сначала пробуем прокси, которые уже были рабочими в прошлом запуске
            if _proxy_manager.count > 0:
                await _proxy_manager.validate_working_pool()
            if _proxy_manager.count < settings.proxy_working_pool_size:
                count = await _proxy_manager.refresh()
                if count > 0:
                    await _proxy_manager.find_working()
            if _proxy_manager.count == 0:
                logger.warning("Рабочий прокси не найден, работаем без прокси")
                _proxy_manager = None
            else:
                logger.info(
                    "Прокси: в пуле %d рабочих, текущий %s",
                    _proxy_manager.count,
                    _proxy_manager.get_current(),
                )

    try:
        bot = Bot(token=settings.bot_token, session=RetryOnFloodSession(proxy_manager=_proxy_manager))
    except TokenValidationError:
        token_preview = settings.bot_token[:10] + "..." if len(settings.bot_token) > 10 else settings.bot_token
        logger.error(
            "BOT_TOKEN невалиден (%r, len=%d). Проверьте формат: ЧИСЛА:БУКВЫ. "
            "Если токен в кавычках в .env — уберите их.",
            token_preview,
            len(settings.bot_token),
        )
        raise SystemExit(1)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(LoggingMiddleware())
    dp.error.register(error_handler)

    # Порядок важен: упоминания должны ловиться до остальных обработчиков
    dp.include_router(help_handler.router)  # mention-help (catch-all, не блокирует)
    dp.include_router(admin.router)  # админ-команды
    dp.include_router(games.router)  # игры (команды /21, /score)
    dp.include_router(forms.router)  # формы с FSM (перед модерацией!)
    dp.include_router(shop.router)  # магазин монет (FSM, перед economy)
    dp.include_router(economy_handler.router)  # инициативы жителей (доработки бота)
    dp.include_router(roulette.router)  # рулетка (команда /bet)
    dp.include_router(suggest.router)   # предложить место в инфраструктуру ЖК
    dp.include_router(text_publish.router)  # отправка текста от лица бота в выбранный топик
    dp.include_router(personalization_handler.router)  # /off_nudges, /on_nudges (только в DM)
    dp.include_router(moderation.router)  # модерация (catch-all, пропускает FSM)
    # stats.router убран — статистика через middleware

    POLLING_MAX_RETRIES = 5
    POLLING_RETRY_DELAYS = (5, 10, 20, 30, 60)

    scheduler: AsyncIOScheduler | None = None
    try:
        # Блокирующая часть: только БД и миграции (несколько секунд).
        await on_startup_critical()
        scheduler = await schedule_jobs(bot)
        # Всё остальное — probes, seed, set_commands, AI probe, стартовое уведомление —
        # в фон, чтобы polling начал принимать сообщения немедленно.
        _run_background_task(on_startup_warmup(bot), name="startup_warmup")
        polling_attempt = 0
        while True:
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                try:
                    await dp.start_polling(
                        bot,
                        allowed_updates=[
                            "message",
                            "edited_message",
                            "callback_query",
                            "message_reaction",
                            "chat_member",
                        ],
                    )
                except TypeError:
                    # Совместимость с тестовыми/облегчёнными dispatcher-реализациями.
                    await dp.start_polling(bot)
                break  # Нормальное завершение polling
            except TelegramNetworkError as exc:
                polling_attempt += 1
                if polling_attempt > POLLING_MAX_RETRIES:
                    logger.error(
                        "Не удалось запустить polling после %d попыток: %s. Завершаем.",
                        POLLING_MAX_RETRIES, exc,
                    )
                    break
                delay = POLLING_RETRY_DELAYS[min(polling_attempt - 1, len(POLLING_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Polling сбой (%s), попытка %d/%d. Повтор через %d сек.",
                    exc, polling_attempt, POLLING_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
            except TelegramAPIError as exc:
                logger.error(
                    "Не удалось запустить polling: ошибка Telegram API (%s). "
                    "Проверьте BOT_TOKEN (len=%d) и настройки бота.",
                    exc,
                    len(settings.bot_token),
                )
                break  # API ошибка (неверный токен) — ретраи бесполезны
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        await close_ai_client()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
