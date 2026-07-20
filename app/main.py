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
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
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
    blackjack as blackjack_handler,
    quiz as quiz_handler,
    forms,
    help as help_handler,
    moderation,
    personalization as personalization_handler,
    suggest,
    text_publish,
)
from app.models import MigrationFlag, UserStat
from app.services.topic_stats import bump_topic_stat
from app.services.health import get_health_state, update_heartbeat, update_notice
from app.services.db_maintenance import cleanup_old_data, optimize_sqlite
from app.utils.time import now_tz
from app.services.ai_module import clear_assistant_cache, close_ai_client, get_ai_client, set_ai_admin_notifier
from app.services.backup import send_db_backup
from app.services.daily_report import send_daily_report
from app.services.place_verify import verify_places
from app.services.unanswered import send_unanswered_digest
from app.services.daily_messages import (
    send_morning_greeting,
    send_presence_morning,
    send_presence_evening,
)
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

class RetryOnFloodSession(AiohttpSession):
    """Повторяет запросы при флуд-контроле и сетевых сбоях Telegram."""

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

            # Миграция places: паспорт достоверности
            if inspector.has_table("places"):
                columns = {
                    column["name"] for column in inspector.get_columns("places")
                }
                if "verified_at" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE places ADD COLUMN verified_at DATETIME")
                    )
                if "verified_by" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE places ADD COLUMN verified_by VARCHAR(120)")
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


async def apply_v12_coins_200(session: AsyncSession) -> None:
    """Единоразово: всем жителям баланс 200 монет — старт ставочного блэкджека.

    Новые пользователи получают 200 через coins.DEFAULT_COINS.
    """
    flag = await session.get(MigrationFlag, "v12_coins_200")
    if flag:
        return

    await session.execute(update(UserStat).values(coins=200))
    session.add(MigrationFlag(key="v12_coins_200"))
    await session.commit()
    logger.info("v1.2: всем начислено по 200 монет (запуск блэкджека)")


async def drop_orphaned_tables(session: AsyncSession) -> None:
    """Удаляет осиротевшие таблицы (quiz/lottery + мёртвые модели v1.2)."""
    flag = await session.get(MigrationFlag, "drop_orphaned_tables_v2")
    if flag:
        return

    # ВНИМАНИЕ: quiz_questions и quiz_sessions СНОВА живые (викторина вернулась,
    # июль 2026) — их здесь быть не должно, иначе на свежей БД create_all их
    # создаст, а миграция сразу снесёт. Дропаем только реально мёртвые таблицы
    # старой викторины/лотереи.
    orphaned = [
        "lottery_tickets",
        "quiz_daily_limits",
        "quiz_used_questions",
        "quiz_user_stats",
        # Модели удалены из кода (мёртвый функционал):
        "resident_services",
        "moderation_calibrations",
    ]
    for table_name in orphaned:
        await session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    session.add(MigrationFlag(key="drop_orphaned_tables_v2"))
    await session.commit()
    logger.info("Удалены осиротевшие таблицы: %s", ", ".join(orphaned))


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


async def _systematize_rag_job() -> None:
    """Ночная систематизация RAG-базы (категории, канонические тексты)."""
    try:
        from app.services.rag import systematize_rag
        async for session in get_session():
            changed = await systematize_rag(session, settings.forum_chat_id)
            if changed:
                await session.commit()
            logger.info("RAG систематизация: обновлено %s записей.", changed)
            return
    except Exception:
        logger.exception("Ошибка ночной систематизации RAG.")


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
    scheduler.add_job(
        heartbeat_job, "interval", minutes=HEARTBEAT_INTERVAL_MIN, args=[bot]
    )
    scheduler.add_job(_cleanup_flood_tracker, "interval", minutes=10)
    scheduler.add_job(
        cleanup_database,
        "cron",
        hour=4,
        minute=20,
    )
    # Систематизация RAG вынесена из hot-path ответа ассистента (см. _get_rag_context):
    # перезапись категорий/канонических текстов — раз в ночь, а не на каждый вопрос.
    scheduler.add_job(
        _systematize_rag_job,
        "cron",
        hour=4,
        minute=40,
    )
    scheduler.add_job(
        _sync_places_from_sheets,
        "cron",
        hour="0,6,12,18",
        minute=30,
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
    # Напоминания «бот отвечает только по запросу»: утро 10:00 и вечер 21:00
    # в главном чате. Подчёркивают, что сам бот в разговоры не встревает.
    scheduler.add_job(
        send_presence_morning, "cron", hour=10, minute=0, args=[bot],
    )
    scheduler.add_job(
        send_presence_evening, "cron", hour=21, minute=0, args=[bot],
    )
    # Ночной бэкап БД в админ-чат (3:30) — офсайт-копия на случай потери сервера.
    scheduler.add_job(
        send_db_backup, "cron", hour=3, minute=30, args=[bot],
    )
    # Петля роста: понедельник 11:00 — дайджест «не знаю»-вопросов админам.
    scheduler.add_job(
        send_unanswered_digest, "cron", day_of_week="mon", hour=11, minute=0, args=[bot],
    )
    # Достоверность инфраструктуры: вторник 12:00 — сверка мест с первоисточниками.
    scheduler.add_job(
        verify_places, "cron", day_of_week="tue", hour=12, minute=0, args=[bot],
    )
    # Вечерняя сводка работы бота в лог-чат (22:30, без звука).
    scheduler.add_job(
        send_daily_report, "cron", hour=22, minute=30, args=[bot],
    )
    # Блэкджек «21» (все джобы сами выходят, если topic_games не задан):
    # таймаут партий, полуночное закрытие + чистка, субботний лидерборд,
    # ежедневный анонс правил перед окном 22:00–00:00.
    from app.handlers.blackjack import (
        announce_game_soon,
        check_game_timeouts,
        close_games_and_cleanup,
        send_weekly_game_leaderboard,
    )
    scheduler.add_job(check_game_timeouts, "interval", minutes=1, args=[bot])
    # Полночь: закрыть партии + топ-5 по монетам за день + чистка сообщений.
    scheduler.add_job(close_games_and_cleanup, "cron", hour=0, minute=0, args=[bot])
    scheduler.add_job(
        send_weekly_game_leaderboard, "cron", day_of_week="sat", hour=21, minute=0, args=[bot],
    )
    # За 5 минут до окна — случайное приглашение соседей на игру.
    scheduler.add_job(announce_game_soon, "cron", hour=21, minute=55, args=[bot])
    # Викторина (все джобы сами выходят при topic_games=None): анонс 19:55,
    # старт 20:00, watchdog каждую минуту (возобновляет driver после рестарта).
    from app.handlers.quiz import announce_quiz_soon, quiz_watchdog, start_quiz_auto
    scheduler.add_job(announce_quiz_soon, "cron", hour=19, minute=55, args=[bot])
    # misfire_grace_time: если бот рестартнул около 20:00, старт не «потеряется»
    # (APScheduler по умолчанию пропускает просроченную джобу) — наверстает в
    # течение 30 минут. Иначе тур молча не начинается (одна из причин «анонс был,
    # игры не было»).
    scheduler.add_job(
        start_quiz_auto, "cron", hour=20, minute=0, args=[bot],
        misfire_grace_time=1800, coalesce=True,
    )
    scheduler.add_job(quiz_watchdog, "interval", minutes=1, args=[bot])
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
            await apply_v12_coins_200(session)
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
        async def _set_commands_with_retry(coro_fn, label: str) -> None:
            """Вызывает coro_fn() с 3 попытками (2 с, 4 с) при сбое."""
            for attempt in range(1, 4):
                try:
                    await coro_fn()
                    return
                except Exception:  # noqa: BLE001
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.warning("Не удалось зарегистрировать %s в Telegram меню.", label)

        async def _set_public_commands() -> None:
            await bot.set_my_commands(
                [
                    BotCommand(command="help", description="Справка и навигация по форуму"),
                    BotCommand(command="rules", description="Правила нашего сообщества"),
                    BotCommand(command="ai", description="Задать вопрос Жаботу"),
                    BotCommand(command="предложить", description="Предложить место в инфраструктуре ЖК"),
                    BotCommand(command="21", description="Блэкджек на монеты (тема Игры, 22:00–00:00)"),
                    BotCommand(command="score", description="Баланс монет и статистика игр"),
                    BotCommand(command="21top", description="Топ игроков по монетам"),
                    BotCommand(command="bonus", description="Ежедневные +10 монет (/бонус)"),
                    BotCommand(command="gift", description="Подарить монеты (/подарить, реплай + сумма)"),
                    BotCommand(command="quiz_top", description="Знатоки викторины (/викторина_топ)"),
                    BotCommand(command="quiz_rules", description="Правила викторины (/викторина_правила)"),
                ],
            )

        async def _set_admin_commands() -> None:
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
                    BotCommand(command="kb_reload", description="Перечитать базу знаний ЖК"),
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

        await asyncio.gather(
            _set_commands_with_retry(_set_public_commands, "публичные команды"),
            _set_commands_with_retry(_set_admin_commands, "админ-команды"),
        )
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

    # ── Seed вопросов викторины из JSON ──────────────────────────────────────
    # Пустая база = игра молча не стартует в 20:00; про это громко сообщает сам
    # start_quiz_auto (алерт в админ-чат с причиной), поэтому тут только лог.
    _step_t = _time.monotonic()
    try:
        from scripts.seed_quiz import seed_quiz_questions
        async for session in get_session():
            quiz_total = await seed_quiz_questions(session)
            await session.commit()
            logger.info("Викторина: в базе %s вопросов.", quiz_total)
            break
    except Exception:
        logger.exception("Ошибка seed викторины.")
    logger.info("⏱ seed_quiz: %.2fs", _time.monotonic() - _step_t)

    # ── Google Sheets — в фон ────────────────────────────────────────────────
    _run_background_task(_sync_places_from_sheets(), name="startup_sync_places")

    # ── AI клиент + probe ────────────────────────────────────────────────────
    _step_t = _time.monotonic()
    get_ai_client()
    ai_probe_note = ""
    if settings.ai_enabled and settings.ai_key:
        _model_roles: dict[str, list[str]] = {}
        for _role, _model in [
            ("main", settings.ai_main_model),
            ("faq", settings.ai_faq_model),
            ("reply", settings.ai_reply_model),
            ("digest", settings.ai_digest_model),
            ("gate_extract", settings.ai_gate_extract_model),
            ("fallback", settings.ai_fallback_model),
            ("classifier", settings.ai_classifier_model),
            ("spam", settings.ai_spam_model),
            ("topic", settings.ai_topic_model),
            ("gate_intent", settings.ai_gate_intent_model),
            ("code", settings.ai_code_model),
            ("premium", settings.ai_premium_model),
        ]:
            _model_roles.setdefault(_model, []).append(_role)
        _model_lines = "\n".join(
            f"  · {_m.split('/')[-1]} ({', '.join(_r)})"
            for _m, _r in _model_roles.items()
        )
        ai_mode = f"AI: Anthropic\n{_model_lines}"
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
    # Туда же — флуд-контроль (RetryAfter) и просроченные колбэки («query is
    # too old»): в вечер запуска игры они засыпали админ-чат десятками копий.
    is_transient_network = (
        isinstance(exc, (TelegramNetworkError, TelegramRetryAfter))
        or (isinstance(exc, TelegramBadRequest) and "query is too old" in str(exc))
    )
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

    try:
        bot = Bot(token=settings.bot_token, session=RetryOnFloodSession())
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
    # Игровой движок: блэкджек «21» со ставками (тема topic_games; выключен,
    # если тема не задана). Экономика — app/services/coins.py, логика —
    # app/services/blackjack.py. Новые игры подключать здесь же (до модерации).
    dp.include_router(blackjack_handler.router)
    # Викторина (тема topic_games, старт 20:00). Роутер ДО модерации: ловит
    # текстовые ответы игроков в теме игр. Модерация эту тему и так исключает.
    dp.include_router(quiz_handler.router)
    dp.include_router(forms.router)  # формы с FSM (перед модерацией!)
    # shop.router и economy_handler.router отключены: магазин и голосования
    # убраны из продукта (июль 2026) и не возвращаются вместе с игрой.
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
        # aiogram сам ловит SIGTERM/SIGINT (handle_signals=True в start_polling)
        # и корректно останавливает polling; здесь — только освобождение ресурсов.
        # wait=False: не ждём завершения бегущих джоб, чтобы docker stop
        # укладывался в stop_grace_period и не получал SIGKILL.
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await close_ai_client()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
