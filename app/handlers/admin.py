"""Почему: админские команды выделены отдельно для контроля доступа."""

from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatPermissions, Message
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import get_session
from app.models import GameState
from app.services.games import can_grant_coins, get_or_create_stats, register_coin_grant
from app.services.strikes import add_strike, clear_strikes
from app.utils.admin import extract_target_user, is_admin
from app.utils.admin_help import (
    ADMIN_CATEGORIES,
    admin_back_keyboard,
    admin_menu_keyboard,
)
from app.handlers.help import clear_routing_state
from app.services.ai_module import (
    get_ai_client,
    get_ai_diagnostics,
    get_ai_runtime_status,
    get_ai_usage_for_today,
    is_ai_runtime_enabled,
    set_ai_runtime_enabled,
    resolve_provider_mode,
)
from app.handlers.moderation import is_training_mode, set_training_mode
from app.services.ai_usage import next_reset_delta
from app.services.rag import add_rag_message, build_canonical_text, get_rag_count, systematize_rag
from app.services.resident_services import (
    add_service,
    deactivate_service,
    get_service_by_source_message_id,
    get_services_count,
    list_services_by_category,
    CATEGORY_LABELS,
)
from app.services.admin_stats_reset import reset_runtime_statistics
from app.utils.profanity import load_profanity, load_profanity_exceptions

router = Router()
logger = logging.getLogger(__name__)


STOP_FLAG = settings.data_dir / ".stopped"


def _admin_label(message: Message) -> str:
    if message.from_user:
        return message.from_user.full_name
    if message.sender_chat:
        return message.sender_chat.title or str(message.sender_chat.id)
    return "неизвестный админ"


def _admin_id(message: Message) -> str:
    if message.from_user:
        return str(message.from_user.id)
    if message.sender_chat:
        return str(message.sender_chat.id)
    return "unknown"


async def _ensure_admin(message: Message, bot: Bot) -> bool:
    if message.from_user is None:
        if message.sender_chat and message.sender_chat.id in {
            settings.forum_chat_id,
            settings.admin_log_chat_id,
        }:
            return True
        return False

    for chat_id in (settings.forum_chat_id, settings.admin_log_chat_id):
        try:
            if await is_admin(bot, chat_id, message.from_user.id):
                return True
        except Exception:  # noqa: BLE001 - не выдаём доступ на ошибке конкретного чата
            logger.exception(
                "Не удалось проверить права администратора в чате %s.", chat_id
            )
    return False


async def _ensure_admin_cb(callback: CallbackQuery, bot: Bot) -> bool:
    """Проверка прав администратора для callback_query."""
    if callback.from_user is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return False
    for chat_id in (settings.forum_chat_id, settings.admin_log_chat_id):
        try:
            if await is_admin(bot, chat_id, callback.from_user.id):
                return True
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось проверить права в чате %s.", chat_id)
    await callback.answer("Только для администраторов.", show_alert=True)
    return False


# ---------------------------------------------------------------------------
#  /admin — интерактивное меню категорий
# ---------------------------------------------------------------------------

_ADM_MENU_TEXT = "⚙️ <b>Панель администратора</b>\nВыберите раздел:"

@router.message(Command("admin"))
async def admin_help(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        if message.sender_chat:
            await message.reply(
                _ADM_MENU_TEXT,
                parse_mode="HTML",
                reply_markup=admin_menu_keyboard(),
            )
        return
    if not await _ensure_admin(message, bot):
        return
    await message.reply(
        _ADM_MENU_TEXT,
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("adm:"))
async def admin_menu_cb(callback: CallbackQuery, bot: Bot) -> None:
    """Обработка нажатий на кнопки админ-меню."""
    if callback.message is None:
        await callback.answer()
        return
    if not await _ensure_admin_cb(callback, bot):
        return

    key = (callback.data or "").split(":", 1)[1]

    if key == "back":
        await callback.message.edit_text(
            _ADM_MENU_TEXT,
            parse_mode="HTML",
            reply_markup=admin_menu_keyboard(),
        )
        await callback.answer()
        return

    category = ADMIN_CATEGORIES.get(key)
    if category is None:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return

    _, text = category
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(Command("mute"))
async def mute_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество минут.")
        return
    try:
        minutes = int(parts[1])
    except ValueError:
        await message.reply("Минуты должны быть числом.")
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    permissions = ChatPermissions(can_send_messages=False)
    await bot.restrict_chat_member(
        settings.forum_chat_id,
        target_id,
        permissions=permissions,
        until_date=until,
    )
    await message.reply(f"Пользователь замьючен на {minutes} минут.")


@router.message(Command("unmute"))
async def unmute_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    permissions = ChatPermissions(can_send_messages=True, can_send_other_messages=True)
    await bot.restrict_chat_member(
        settings.forum_chat_id, target_id, permissions=permissions
    )
    await message.reply("Мут снят.")


@router.message(Command("ban"))
async def ban_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество дней.")
        return
    try:
        days = int(parts[1])
    except ValueError:
        await message.reply("Дни должны быть числом.")
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    until = datetime.now(timezone.utc) + timedelta(days=days)
    await bot.ban_chat_member(settings.forum_chat_id, target_id, until_date=until)
    await message.reply(f"Бан на {days} дней выдан.")


@router.message(Command("unban"))
async def unban_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    await bot.unban_chat_member(settings.forum_chat_id, target_id)
    await message.reply("Бан снят.")


@router.message(Command("strike"))
async def strike_user(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    async for session in get_session():
        count = await add_strike(session, target_id, settings.forum_chat_id)
        await session.commit()
    if count >= 3:
        until = datetime.now(timezone.utc) + timedelta(hours=24)
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            settings.forum_chat_id,
            target_id,
            permissions=permissions,
            until_date=until,
        )
        async for session in get_session():
            await clear_strikes(session, target_id, settings.forum_chat_id)
            await session.commit()
        await message.reply("Третий страйк! Мут на 24 часа.")
        return
    await message.reply(f"Страйк добавлен. Всего: {count}")


@router.message(Command("addcoins"))
async def grant_coins(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Укажи количество монет.")
        return
    try:
        amount = int(parts[1])
    except ValueError:
        await message.reply("Монеты должны быть числом.")
        return
    target_id, display_name = extract_target_user(message)
    if target_id is None:
        await message.reply("Нужен реплай на сообщение пользователя.")
        return
    async for session in get_session():
        stats = await get_or_create_stats(
            session,
            target_id,
            settings.forum_chat_id,
            display_name=display_name,
        )
        now = datetime.now(timezone.utc)
        if not can_grant_coins(stats, now, amount):
            await message.reply("Нельзя выдать больше 10 монет за раз/сутки.")
            return
        register_coin_grant(stats, now, amount)
        await session.commit()
    await message.reply(f"Начислено {amount} монет.")




@router.message(Command("ai_on"))
async def ai_on(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    set_ai_runtime_enabled(True)
    await message.reply("Флаг runtime включен. При наличии AI_KEY бот будет использовать внешний AI-провайдер.")


@router.message(Command("ai_off"))
async def ai_off(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    set_ai_runtime_enabled(False)
    await message.reply("Runtime-флаг выключен. AI-функции будут работать через локальный fallback.")


@router.message(Command("ai_status"))
async def ai_status(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    status = "запрошено включение" if is_ai_runtime_enabled() else "выключен"
    req_used, tok_used = await get_ai_usage_for_today(settings.forum_chat_id)
    runtime = get_ai_runtime_status()
    last_error = runtime.last_error or "нет"
    if runtime.last_error_at:
        last_error = f"{last_error} ({runtime.last_error_at.isoformat(timespec='seconds')} UTC)"

    provider = "Remote API" if resolve_provider_mode() == "remote" else "STUB"
    training_status = "вкл 🔍" if is_training_mode() else "выкл"
    await message.reply(
        "Статус AI:\n"
        f"• Провайдер: {provider}\n"
        f"• Runtime флаг: {status}\n"
        f"• Режим обучения: {training_status}\n"
        f"• Usage сегодня: запросы={req_used}, токены={tok_used}\n"
        f"• До сброса лимитов: {next_reset_delta()}\n"
        f"• Последний статус: {last_error}"
    )



@router.message(Command("ai_probe"))
async def ai_probe(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    report = await get_ai_diagnostics(settings.forum_chat_id)
    status = "✅" if report.probe_ok else "⚠️"
    await message.reply(
        "AI probe (3 слоя):\n"
        f"1) Конфиг: ai_enabled={report.ai_enabled}, ai_key={'set' if report.has_api_key else 'empty'}, "
        f"provider={report.provider_mode}, api_url={report.api_url}\n"
        f"2) Реальный вызов: {status} {report.probe_details} (latency={report.probe_latency_ms} ms)\n"
        f"3) Учёт usage сегодня: requests={report.requests_used_today}, tokens={report.tokens_used_today}"
    )



@router.message(Command("training_on"))
async def training_on(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    set_training_mode(True)
    await message.reply(
        "🔍 Режим обучения включён.\n"
        "Бот НЕ будет модерировать — только отправлять подозрительные сообщения "
        "в лог-чат с кнопками для подтверждения действия."
    )


@router.message(Command("training_off"))
async def training_off(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    set_training_mode(False)
    await message.reply("✅ Режим обучения выключен. Модерация работает в штатном режиме.")


@router.message(Command("reload_profanity"))
async def reload_profanity(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return
    words = load_profanity()
    exceptions = load_profanity_exceptions()
    await message.reply(
        "Словари перечитаны с диска. "
        f"Мат-словарь: {len(words)}, исключения: {len(exceptions)}."
    )


@router.message(Command("reset_routing_state"))
async def reset_routing_state(message: Message, bot: Bot) -> None:
    if not await _ensure_admin(message, bot):
        return

    target_id, display_name = extract_target_user(message)
    parts = (message.text or "").split(maxsplit=1)
    if target_id is None and len(parts) > 1:
        raw_target = parts[1].strip()
        if raw_target.startswith("@"):
            try:
                chat = await bot.get_chat(raw_target)
            except Exception:  # noqa: BLE001 - Telegram API может ответить ошибкой
                chat = None
            target_id = chat.id if chat else None
            display_name = raw_target
        elif raw_target.isdigit():
            target_id = int(raw_target)
            display_name = raw_target

    if target_id is None:
        cleared = clear_routing_state()
        await message.reply(f"Сброшено ожиданий: {cleared}.")
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Админ {_admin_id(message)} сбросил все ожидания /help.",
        )
        return

    cleared = clear_routing_state(user_id=target_id, chat_id=settings.forum_chat_id)
    await message.reply(
        f"Ожидание для пользователя {display_name or target_id} сброшено."
    )
    if cleared:
        await bot.send_message(
            settings.admin_log_chat_id,
            f"Админ {_admin_id(message)} сбросил ожидание /help для {target_id}.",
        )


@router.message(Command("reset_stats"))
async def reset_stats(message: Message, bot: Bot) -> None:
    """Обнуляет статистику игр, не затрагивая базу знаний RAG."""
    if not await _ensure_admin(message, bot):
        return

    cleared: list[str] = []

    async for session in get_session():
        deleted_rows = await reset_runtime_statistics(session)
        if deleted_rows["user_stats"] > 0:
            cleared.append(f"статистика игры 21 ({deleted_rows['user_stats']})")
        if deleted_rows["game_states"] > 0:
            cleared.append(f"активные игры 21 ({deleted_rows['game_states']})")

        await session.commit()


    if cleared:
        await message.reply("Статистика и сессии сброшены: " + ", ".join(cleared) + "\nRAG-база не изменялась.")
    else:
        await message.reply("Статистика уже пустая, сессия сброшена. RAG-база не изменялась.")


@router.message(Command("restart_jobs"))
async def restart_jobs(message: Message, bot: Bot, state: FSMContext) -> None:
    """Останавливает зависшие задачи (формы и игры)."""
    if not await _ensure_admin(message, bot):
        return

    cleared = []

    # 1. Очищаем БД
    async for session in get_session():
        # Игры
        result = await session.execute(delete(GameState))
        if result.rowcount > 0:
            cleared.append(f"игры ({result.rowcount})")

        await session.commit()

    # 2. Очищаем FSM (через storage)
    storage = state.storage
    # MemoryStorage хранит данные в _data dict
    if hasattr(storage, "_data"):
        storage._data.clear()
        cleared.append("FSM-состояния")

    if cleared:
        await message.reply(f"Очищено: {', '.join(cleared)}")
    else:
        await message.reply("Нет зависших задач.")


_SERVICES_TOPIC_ID = 3240


@router.message(Command("usluga"))
async def usluga_command(message: Message, bot: Bot) -> None:
    """Добавляет услугу от жителя в каталог. Только для админов, только в топике услуг."""
    if not await _ensure_admin(message, bot):
        return

    # Проверяем, что команда вызвана в топике услуг
    services_topic = getattr(settings, "topic_services", None) or _SERVICES_TOPIC_ID
    if message.message_thread_id != services_topic:
        await message.reply(
            "Команда /usluga работает только в топике «Услуги от жителей ЖК»."
        )
        return

    if message.reply_to_message is None:
        await message.reply(
            "Используйте /usluga как реплай на сообщение жителя с описанием услуги."
        )
        return

    target_msg = message.reply_to_message
    text = target_msg.text or target_msg.caption
    if not text or len(text.strip()) < 5:
        await message.reply("Сообщение слишком короткое для добавления в каталог услуг.")
        return

    async for session in get_session():
        existing = await get_service_by_source_message_id(
            session,
            chat_id=settings.forum_chat_id,
            source_message_id=target_msg.message_id,
        )
        if existing is not None:
            await message.reply(
                "Эта услуга уже есть в каталоге.\n"
                f"Категория: {existing.category}\n"
                f"Описание: {existing.description[:200]}"
            )
            return

    admin_id = int(_admin_id(message))
    provider_user_id = target_msg.from_user.id if target_msg.from_user else 0
    provider_name = target_msg.from_user.full_name if target_msg.from_user else None

    # AI-категоризация услуги
    ai_description = None
    ai_keywords = None
    ai_category = None
    ai_client = get_ai_client()
    try:
        from app.services.ai_module import _strip_think_tags
        provider = ai_client._provider
        if hasattr(provider, "_chat_completion"):
            import json as _json
            content, _ = await provider._chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты систематизируешь услуги от жителей ЖК. Верни только JSON:\n"
                            '{"description":"краткое описание услуги до 200 символов",'
                            '"keywords":"ключевые слова через запятую для поиска",'
                            '"category":"кондитерская|красота|ремонт|обучение|дети|авто|здоровье|уборка|доставка|фото_видео|IT|юридические|рукоделие|общее"}\n'
                            "Description — перефразируй суть услуги кратко и понятно.\n"
                            "Keywords — слова, по которым житель мог бы найти эту услугу.\n"
                            "Category — одна из перечисленных категорий."
                        ),
                    },
                    {"role": "user", "content": text.strip()[:2000]},
                ],
                chat_id=settings.forum_chat_id,
            )
            data = _json.loads(_strip_think_tags(content))
            ai_description = str(data.get("description", ""))[:500] or None
            ai_keywords = str(data.get("keywords", ""))[:1000] or None
            ai_category = str(data.get("category", ""))[:100] or None
    except Exception:
        logger.warning("AI-категоризация услуги не удалась, используем локальный fallback.")

    async for session in get_session():
        try:
            record = await add_service(
                session,
                chat_id=settings.forum_chat_id,
                message_text=text.strip(),
                provider_user_id=provider_user_id,
                provider_name=provider_name,
                source_message_id=target_msg.message_id,
                added_by_user_id=admin_id,
                ai_description=ai_description,
                ai_keywords=ai_keywords,
                ai_category=ai_category,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await get_service_by_source_message_id(
                session,
                chat_id=settings.forum_chat_id,
                source_message_id=target_msg.message_id,
            )
            if existing is not None:
                await message.reply(
                    "Эта услуга уже была добавлена параллельно другим админом.\n"
                    f"Категория: {existing.category}\n"
                    f"Описание: {existing.description[:200]}"
                )
                return
            raise
        count = await get_services_count(session, settings.forum_chat_id)

    cat_label = record.category
    ai_mark = " (AI)" if ai_description else ""
    kw_preview = (record.keywords or "")[:150]
    await message.reply(
        f"✅ Услуга добавлена в каталог!\n"
        f"Категория: {cat_label}{ai_mark}\n"
        f"Описание: {record.description[:200]}\n"
        f"Теги: {kw_preview}\n"
        f"Всего услуг: {count}\n\n"
        f"Удалить: /del_usluga (реплаем на то же сообщение)"
    )

    # Уведомляем провайдера о добавлении услуги
    if provider_user_id:
        cat_display = CATEGORY_LABELS.get(record.category, record.category)
        try:
            await bot.send_message(
                settings.forum_chat_id,
                f"✅ Ваша услуга добавлена в каталог ЖК!\n"
                f"Категория: {cat_display}\n"
                f"Описание: {record.description[:200]}\n\n"
                f"Жители смогут найти вас через поиск и при вопросах в чате.",
                message_thread_id=message.message_thread_id,
                reply_to_message_id=target_msg.message_id,
            )
        except Exception:
            logger.warning("Не удалось уведомить провайдера %s об услуге", provider_user_id)

    logger.info(
        "USLUGA: админ %s добавил услугу от %s (msg=%s, категория=%s)",
        admin_id, provider_user_id, target_msg.message_id, record.category,
    )


@router.message(Command("del_usluga"))
async def del_usluga_command(message: Message, bot: Bot) -> None:
    """Деактивирует услугу из каталога. Только для админов, реплаем на исходное сообщение."""
    if not await _ensure_admin(message, bot):
        return

    if message.reply_to_message is None:
        await message.reply(
            "Используйте /del_usluga как реплай на сообщение жителя, чья услуга добавлена в каталог."
        )
        return

    target_msg = message.reply_to_message
    async for session in get_session():
        existing = await get_service_by_source_message_id(
            session,
            chat_id=settings.forum_chat_id,
            source_message_id=target_msg.message_id,
        )
        if existing is None:
            await message.reply("Услуга по этому сообщению не найдена в каталоге.")
            return
        await deactivate_service(session, existing.id)
        await session.commit()

    await message.reply(
        f"🗑 Услуга удалена из каталога.\n"
        f"Была категория: {existing.category}\n"
        f"Описание: {existing.description[:150]}"
    )
    logger.info(
        "DEL_USLUGA: админ %s удалил услугу (msg=%s)",
        _admin_id(message), target_msg.message_id,
    )


@router.message(Command("rag_bot"))
async def rag_bot_command(message: Message, bot: Bot) -> None:
    """Добавляет сообщение (реплай) в RAG-базу знаний бота."""
    if not await _ensure_admin(message, bot):
        return

    if message.reply_to_message is None:
        await message.reply(
            "Используйте /rag_bot как реплай на сообщение, "
            "которое хотите добавить в базу знаний бота."
        )
        return

    target_msg = message.reply_to_message
    text = target_msg.text or target_msg.caption
    if not text or len(text.strip()) < 10:
        await message.reply("Сообщение слишком короткое или пустое для базы знаний.")
        return

    admin_id = int(_admin_id(message))
    source_user_id = target_msg.from_user.id if target_msg.from_user else None

    # LLM-категоризация перед добавлением
    ai_client = get_ai_client()
    cat_result = await ai_client.categorize_rag_entry(
        text.strip(), chat_id=settings.forum_chat_id,
    )

    async for session in get_session():
        record = await add_rag_message(
            session,
            chat_id=settings.forum_chat_id,
            message_text=text.strip(),
            added_by_user_id=admin_id,
            source_user_id=source_user_id,
            source_message_id=target_msg.message_id,
        )
        # Преобразуем запись в канонический вид без отдельной приоритизации.
        record.rag_category = cat_result.category
        record.rag_canonical_text = build_canonical_text([cat_result.summary or text.strip()])
        await systematize_rag(session, settings.forum_chat_id)
        await session.commit()
        count = await get_rag_count(session, settings.forum_chat_id)

    cat_label = f"{cat_result.category}"
    if not cat_result.used_fallback:
        cat_label += " (AI)"
    await message.reply(
        f"Сообщение добавлено в базу знаний бота.\n"
        f"Категория: {cat_label}\n"
        f"Всего записей в базе: {count}"
    )
    logger.info(
        "RAG: админ %s добавил сообщение %s в базу знаний (категория=%s, llm=%s)",
        _admin_id(message),
        target_msg.message_id,
        cat_result.category,
        not cat_result.used_fallback,
    )


@router.message(Command("rag_sync"))
async def rag_sync_command(message: Message, bot: Bot) -> None:
    """Пересобирает и сохраняет систематизированную RAG-базу."""
    if not await _ensure_admin(message, bot):
        return

    async for session in get_session():
        changed = await systematize_rag(session, settings.forum_chat_id)
        await session.commit()
        count = await get_rag_count(session, settings.forum_chat_id)

    await message.reply(
        "База знаний пересобрана.\n"
        f"Обновлено записей: {changed}\n"
        f"Всего записей: {count}"
    )



@router.message(Command("shutdown_bot"))
async def shutdown_bot_cmd(message: Message, bot: Bot) -> None:
    """Полностью останавливает бота без автоматического перезапуска."""
    if not await _ensure_admin(message, bot):
        return

    # Создаём файл-флаг для предотвращения перезапуска
    STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.touch()

    await message.reply("🛑 Бот останавливается...")
    await bot.send_message(
        settings.admin_log_chat_id,
        f"🛑 Бот остановлен командой /shutdown_bot\n"
        f"Админ: {_admin_label(message)}\n"
        f"Для запуска: удалить {STOP_FLAG} и перезапустить контейнер",
    )

    # Отправляем сигнал завершения процессу
    os.kill(os.getpid(), signal.SIGTERM)
