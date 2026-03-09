"""Почему: главный модуль собирает роутеры, БД и планировщик в одном месте."""

from __future__ import annotations

import asyncio
import logging
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
from app.handlers import admin, forms, games, help as help_handler, moderation, quiz, roulette
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
from app.services.ai_module import close_ai_client, get_ai_client, set_ai_admin_notifier
from app.services.daily_summary import build_daily_summary, render_daily_summary
from app.services.resident_kb import load_resident_kb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

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


class RetryOnFloodSession(AiohttpSession):
    """Автоматически повторяет запросы при TelegramRetryAfter (flood control)."""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        for attempt in range(1, MAX_RETRIES_ON_FLOOD + 1):
            try:
                return await super().make_request(bot, method, timeout=timeout)
            except TelegramRetryAfter as e:
                if attempt == MAX_RETRIES_ON_FLOOD:
                    raise
                logger.warning(
                    "Flood control, жду %s сек (попытка %d/%d)",
                    e.retry_after, attempt, MAX_RETRIES_ON_FLOOD,
                )
                await asyncio.sleep(e.retry_after)


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
                columns = {
                    column["name"] for column in inspector.get_columns("user_stats")
                }
                if "display_name" not in columns:
                    sync_conn.execute(
                        text("ALTER TABLE user_stats ADD COLUMN display_name TEXT")
                    )

            # Миграция quiz_sessions
            if inspector.has_table("quiz_sessions"):
                columns = {
                    column["name"]: column
                    for column in inspector.get_columns("quiz_sessions")
                }
                if "used_question_ids" not in columns:
                    sync_conn.execute(
                        text(
                            "ALTER TABLE quiz_sessions ADD COLUMN used_question_ids TEXT"
                        )
                    )
                is_active_column = columns.get("is_active")
                if (
                    is_active_column
                    and sync_conn.dialect.name == "postgresql"
                    and isinstance(is_active_column["type"], Integer)
                ):
                    sync_conn.execute(
                        text(
                            "ALTER TABLE quiz_sessions "
                            "ALTER COLUMN is_active "
                            "TYPE BOOLEAN USING is_active::boolean"
                        )
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


async def send_daily_summary(bot: Bot) -> None:
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

    for attempt in range(1, 4):
        try:
            await bot.send_message(
                target_chat_id,
                stats_text,
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
                except TelegramNetworkError as exc:
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


async def schedule_jobs(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        send_daily_summary,
        "cron",
        hour=settings.ai_summary_hour,
        minute=settings.ai_summary_minute,
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
    # Викторина: анонс → правила → автостарт
    scheduler.add_job(
        quiz.announce_quiz_soon,
        "cron",
        hour=19,
        minute=55,
        args=[bot],
    )
    scheduler.add_job(
        quiz.announce_quiz_rules,
        "cron",
        hour=19,
        minute=59,
        args=[bot],
    )
    scheduler.add_job(
        quiz.start_quiz_auto,
        "cron",
        hour=20,
        minute=0,
        args=[bot],
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
    scheduler.start()
    return scheduler


async def on_startup(bot: Bot) -> None:
    async def _admin_notifier(text: str) -> None:
        await bot.send_message(settings.admin_log_chat_id, f"⚠️ {text}")

    set_ai_admin_notifier(_admin_notifier)
    telegram_available = False
    for attempt in range(1, 4):
        try:
            await bot.get_me()  # заполняет bot.me с информацией о боте
            telegram_available = True
            break
        except TelegramNetworkError:
            if attempt >= 3:
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
            logger.error(
                "Не удалось получить данные бота из Telegram API: %s. "
                "Проверьте BOT_TOKEN и права доступа.",
                exc,
            )
            break
    await init_db(engine)
    try:
        await cleanup_database()
    except Exception:  # noqa: BLE001 - не блокируем старт из-за не-критичной очистки
        logger.exception("Очистка БД при старте завершилась с ошибкой.")
    # Применяем миграции
    async for session in get_session():
        await apply_v11_stats_reset(session)
    await heartbeat_job(bot)
    if telegram_available:
        await bot.set_my_commands(
            [
                BotCommand(command="admin", description="Справка по админ-командам"),
                BotCommand(command="mute", description="Мут пользователя (реплай)"),
                BotCommand(command="unmute", description="Снять мут (реплай)"),
                BotCommand(command="ban", description="Бан пользователя (реплай)"),
                BotCommand(command="unban", description="Снять бан (реплай)"),
                BotCommand(command="strike", description="Добавить страйк (реплай)"),
                BotCommand(command="addcoins", description="Выдать монеты (реплай)"),
                BotCommand(
                    command="reload_profanity", description="Обновить список матов"
                ),
                BotCommand(
                    command="restart_jobs", description="Сбросить зависшие задачи"
                ),
                BotCommand(
                    command="reset_routing_state", description="Сбросить ожидание /help"
                ),
                BotCommand(command="shutdown_bot", description="Остановить бота"),
                BotCommand(
                    command="rag_bot", description="Добавить сообщение в базу знаний (реплай)"
                ),
            ],
            scope=BotCommandScopeChatAdministrators(chat_id=settings.forum_chat_id),
        )
    # Проверяем и прогреваем каноническую базу знаний жителей
    try:
        load_resident_kb()
    except Exception:
        logger.exception("Не удалось загрузить базу знаний жителей (resident_kb.json).")

    # Seed инфраструктуры из JSON (если таблица пустая)
    try:
        from scripts.seed_places import seed_places
        async for session in get_session():
            seeded = await seed_places(session)
            if seeded:
                await session.commit()
                logger.info("Seed инфраструктуры: добавлено %s объектов.", seeded)
    except Exception:
        logger.exception("Ошибка seed инфраструктуры.")

    # Импорт инфраструктуры из Google Sheets (если настроен сервисный аккаунт)
    await _sync_places_from_sheets()

    # Возобновляем рулетку, если бот перезагрузился в игровое время
    await roulette.resume_roulette_if_needed(bot)

    # Инициализируем AI-клиент и логируем режим работы
    get_ai_client()
    if settings.ai_enabled and settings.ai_key:
        source_note = " (по умолчанию, AI_MODEL не задан)" if settings.ai_model_is_default else ""
        ai_mode = f"AI: OpenRouter ({settings.ai_model}){source_note}"
    elif not settings.ai_enabled:
        ai_mode = "AI: отключен (AI_ENABLED=false)"
    else:
        ai_mode = "AI: отключен (AI_KEY не задан)"
    logger.info("AI модуль: %s", ai_mode)
    if telegram_available:
        await bot.send_message(
            settings.admin_log_chat_id,
            f"🟢 Бот запущен\nВерсия: {settings.build_version}\n{ai_mode}",
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
        error_text += "\n\nКонтекст:\n"
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
        logger.info("Бот остановлен. Удалите %s для запуска.", STOP_FLAG)
        return

    try:
        bot = Bot(token=settings.bot_token, session=RetryOnFloodSession())
    except TokenValidationError:
        logger.error(
            "BOT_TOKEN невалиден (%r). Проверьте формат: ЧИСЛА:БУКВЫ. "
            "Если токен в кавычках в .env — уберите их.",
            settings.bot_token[:10] + "..." if len(settings.bot_token) > 10 else settings.bot_token,
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
    dp.include_router(quiz.router)  # викторина (команды /umnij_start, /bal, /topumnij)
    dp.include_router(roulette.router)  # рулетка (команда /bet)
    dp.include_router(moderation.router)  # модерация (catch-all, пропускает FSM)
    # stats.router убран — статистика через middleware

    scheduler: AsyncIOScheduler | None = None
    try:
        await on_startup(bot)
        scheduler = await schedule_jobs(bot)
        try:
            await dp.start_polling(bot)
        except TelegramNetworkError as exc:
            logger.error(
                "Не удалось запустить polling: нет доступа к Telegram API (%s)",
                exc,
            )
        except TelegramAPIError as exc:
            logger.error(
                "Не удалось запустить polling: ошибка Telegram API (%s). "
                "Проверьте BOT_TOKEN и настройки бота.",
                exc,
            )
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        await close_ai_client()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
