"""Почему: централизуем интерактивную справку, чтобы не плодить флуд в темах."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    User,
)

from app.config import settings
from app.db import get_session
from app.services.ai_module import get_ai_client, _normalize_cache_key
from app.services.chat_history import (
    load_context,
    save_exchange,
    get_messages_for_compression,
    replace_with_summary,
)
from app.services.faq import get_faq_answer, track_question, update_faq_rating
from app.services.feedback import save_feedback
from app.utils.admin import is_admin
from app.utils.admin_help import ADMIN_HELP

logger = logging.getLogger(__name__)
router = Router()
_BOT_PROFILE_CACHE: User | None = None
_ASSISTANT_CHAT_IDS = {settings.forum_chat_id, settings.admin_log_chat_id}


async def _get_bot_profile(bot: Bot) -> User:
    """Почему: снижаем число вызовов Telegram API при частых упоминаниях."""

    global _BOT_PROFILE_CACHE
    if _BOT_PROFILE_CACHE is None:
        _BOT_PROFILE_CACHE = await bot.get_me()
    return _BOT_PROFILE_CACHE


class HelpRoutingActiveFilter(BaseFilter):
    """Почему: ограничиваем обработчик /help только на активные ожидания."""

    async def __call__(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        key = _state_key(message.chat.id, message.from_user.id)
        return key in HELP_ROUTING_STATE


class BotMentionFilter(BaseFilter):
    """Почему: ловим упоминания бота, не блокируя остальные команды."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.from_user and message.from_user.is_bot:
            return False
        text = _get_message_text(message)
        if text is None:
            return False
        entities = _get_message_entities(message)
        if not text and not entities:
            return False
        me = await _get_bot_profile(bot)
        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == me.id
        )
        return _is_bot_mentioned(message, me) or _is_bot_name_called(text, me) or is_reply_to_bot


HELP_MENU_TEXT = (
    "Я подсказываю, где обсуждать вопросы, и отвечаю на упоминания.\n\n"
    "Выберите тему форума или воспользуйтесь советником «Куда писать?»."
)
HELP_WAIT_TEXT = (
    "Опишите кратко, о чём ваш вопрос, одним сообщением. "
    "Я подскажу, в какой тематический топик лучше его отправить."
)
HELP_TIMEOUT_TEXT = (
    "Вы не ответили в течение 2 минут. Если нужна помощь с темой, "
    "нажмите /help снова."
)
HELP_RATE_LIMIT_TEXT = (
    "Подсказки слишком частые. Пожалуйста, подождите 30 секунд и попробуйте снова."
)
AI_RATE_LIMIT_TEXT = "Слишком часто 🙌 Подождите 20 секунд и повторите вопрос."

MENTION_REPLIES = [
    "Я тут, на посту! Проверяю, чтобы котики не получили бан по ошибке.",
    "Шлифую правила, полирую шлагбаум — всё под контролем!",
    "Считаю монеты, чтобы не убежали из банка.",
    "Охочусь на флуд. Пока что флуд прячется!",
    "Тестирую шутки. Эта прошла контроль качества.",
    "Слежу, чтобы объявления не убежали в оффтоп.",
    "Делаю вид, что отдыхаю. На самом деле модерирую.",
    "Полирую игровые карты. Блэкджек ждёт!",
    "Проверяю, кто забыл сказать «доброе утро».",
    "Сканирую чат на предмет мемов. Всё стабильно.",
    "Отвечаю на упоминания. Это моя суперсила.",
    "Поднимаю щит модерации, но улыбаюсь по-дружески.",
    "Проверяю, чтобы соседям было уютно, как в тапочках.",
    "Разгоняю пыль в чате, чтобы было чисто и весело.",
    "Взвешиваю монеты на улыбках — баланс идеален.",
    "Ищу потерянные мемы. Если найду — не отдам.",
    "Дежурю у шлагбаума, но по совместительству комик.",
    "Контролирую очередность тем. Порядок — моё второе имя.",
    "Пишу заметки о хорошем настроении. Записал твоё.",
    "Разминаю алгоритмы, чтобы отвечать быстрее.",
    "Приглядываю за чатом, как кот за окном.",
    "Строю мосты между темами, чтобы никто не потерялся.",
    "Охраняю тишину в ночи, чтобы всем сладко спалось.",
    "Сортирую реплики по уровню улыбок. Ты в топе.",
    "Тренируюсь ставить мут одним взглядом.",
    "Пересчитываю монеты. У кого-то их скоро будет больше, чем тараканов в подвале.",
    "Заряжаю банхаммер. На всякий случай.",
    "Держу в тонусе тех, кто думает, что правила не для них.",
    "Полирую кнопку от шлагбаума. А то тут некоторые слишком умные.",
    "Ищу, куда бы подевать очередную рекламу потолочника.",
    "Записываю тех, кто обещал прийти на субботник. И не пришел.",
    "Ищу, кому бы выписать предупреждение. Ты, кстати, ничего такого не писал?",
    "Работаю. В отличие от тебя",
    "Объясняю шлагбауму, что не все водители читали ПДД. Он в шоке.",
    "Составляю чек-лист «как припарковаться на трёх местах сразу»",
]

CALLBACK_PREFIX = "help"
CALLBACK_BACK = f"{CALLBACK_PREFIX}:back"
CALLBACK_WHERE = f"{CALLBACK_PREFIX}:where"
CALLBACK_TOPIC = f"{CALLBACK_PREFIX}:topic"
FEEDBACK_PREFIX = "ai_fb"

WAITING_TIMEOUT = timedelta(minutes=2)
HINT_COOLDOWN = timedelta(seconds=30)
HELP_DELETE_TIMEOUT = timedelta(minutes=2)
AI_MENTION_COOLDOWN = timedelta(seconds=20)
MENTION_QUEUE: deque[str] = deque(MENTION_REPLIES)


def _next_mention_reply() -> str:
    value = MENTION_QUEUE[0]
    MENTION_QUEUE.rotate(-1)
    return value


@dataclass
class HelpRoutingState:
    chat_id: int
    user_id: int
    message_id: int
    message_thread_id: int | None
    started_at: datetime


TOPIC_DESCRIPTIONS: dict[str, str] = {
    "Шлагбаум": (
        "Шлагбаум — топик для обсуждения въезда/выезда авто, пропусков, "
        "работы оборудования и доступа на территорию ЖК."
    ),
    "Ремонт": (
        "Ремонт — обсуждаем ремонт квартир, выбор мастеров и материалов, "
        "делимся опытом отделки."
    ),
    "Жалобы": (
        "Жалобы — сюда можно писать о проблемах с сервисом, шумом, уборкой, "
        "неисправностями и прочими претензиями."
    ),
    "Барахолка": (
        "Барахолка — объявления о продаже, покупке, обмене и отдаче вещей."
    ),
    "Питомцы": (
        "Питомцы — всё про собак, кошек и других животных: поиск, уход, "
        "вопросы к ветеринарам."
    ),
    "Мамы и папы": (
        "Мамы и папы — обсуждения детей, школ, садиков, детских площадок и "
        "семейных вопросов."
    ),
    "Недвижимость": (
        "Недвижимость — вопросы покупки, продажи, аренды квартир и работы с риэлторами."
    ),
    "Попутчики": (
        "Попутчики — ищем попутчиков, делимся маршрутами, обсуждаем каршеринг и такси."
    ),
    "Услуги": (
        "Услуги — предложения и запросы услуг: мастера, няни, уборка, ремонт техники."
    ),
    "Правила": (
        "Правила — краткое резюме правил форума. "
        "Полный свод правил опубликован в теме «Правила» — обязательно ознакомьтесь."
    ),
}

TOPIC_ORDER = [
    "Шлагбаум",
    "Ремонт",
    "Жалобы",
    "Барахолка",
    "Питомцы",
    "Мамы и папы",
    "Недвижимость",
    "Попутчики",
    "Услуги",
    "Правила",
]

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "Шлагбаум": ["шлагбаум", "пропуск", "проезд", "въезд", "ворота", "пульт", "карта доступа"],
    "Ремонт": [
        "ремонт",
        "строител",
        "ремонтник",
        "отделк",
        "плитка",
        "ламинат",
        "сантехник",
        "электрик",
    ],
    "Жалобы": [
        "жалоб",
        "претенз",
        "не работ",
        "управляющ",
        "ук",
        "лифт",
        "подъезд",
        "двор",
        "сломал",
        "течёт",
        "шум",
        "грязно",
        "холодно",
    ],
    "Барахолка": [
        "продам",
        "куплю",
        "отдам",
        "даром",
        "обмен",
        "продаю",
        "продается",
        "барахолка",
        "объявление",
        "объявлен",
        "б/у",
    ],
    "Питомцы": [
        "кот",
        "кошка",
        "котик",
        "котён",
        "собак",
        "пёс",
        "щенок",
        "ветеринар",
        "питом",
        "прививк",
        "корм",
        "потерялся",
    ],
    "Мамы и папы": [
        "ребёнок",
        "дети",
        "школа",
        "садик",
        "коляска",
        "мамочк",
        "пап",
        "игрушк",
    ],
    "Недвижимость": [
        "квартира",
        "продажа",
        "купить",
        "сдать",
        "аренда",
        "риэлтор",
        "ипотека",
    ],
    "Попутчики": [
        "поеду",
        "еду",
        "поехать",
        "подвезти",
        "попутчик",
        "такси",
        "каршеринг",
        "доехать",
        "в аэропорт",
    ],
    "Услуги": [
        "услуги",
        "мастер",
        "предлагаю",
        "починю",
        "ремонтирую",
        "уборка",
        "няня",
        "репетитор",
    ],
    "Правила": [],
}

TOPIC_THREADS: dict[str, int | None] = {
    "Шлагбаум": settings.topic_gate,
    "Ремонт": settings.topic_repair,
    "Жалобы": settings.topic_complaints,
    "Барахолка": settings.topic_market,
    "Питомцы": settings.topic_pets,
    "Мамы и папы": settings.topic_parents,
    "Недвижимость": settings.topic_realty,
    "Попутчики": settings.topic_rides,
    "Услуги": settings.topic_services,
    "Правила": settings.topic_rules,
    "Курилка": settings.topic_smoke,
}

HELP_ROUTING_STATE: dict[tuple[int, int], HelpRoutingState] = {}
HELP_TIMEOUT_TASKS: dict[tuple[int, int], asyncio.Task[None]] = {}
LAST_HINT_TIME: dict[tuple[int, int], datetime] = {}
HELP_DELETE_TASKS: dict[tuple[int, int], asyncio.Task[None]] = {}
# In-memory кэш используется как быстрый fallback; основная история — в БД
AI_CHAT_HISTORY: dict[tuple[int, int], deque[str]] = {}
AI_CHAT_HISTORY_LIMIT = 20
LAST_AI_REPLY_TIME: dict[tuple[int, int], datetime] = {}
# Кэш промпт→ответ для feedback кнопок (message_id → данные)
_PENDING_FEEDBACK: dict[int, tuple[int, int, str, str, str]] = {}  # msg_id → (chat_id, user_id, prompt, reply, question_key)


async def _get_menu_text(bot: Bot, user_id: int | None) -> str:
    """Возвращает текст меню, добавляя админ-справку при необходимости."""
    if user_id is None:
        return HELP_MENU_TEXT
    try:
        if await is_admin(bot, settings.forum_chat_id, user_id):
            return f"{HELP_MENU_TEXT}\n\n{ADMIN_HELP}"
    except Exception:  # noqa: BLE001 - не ломаем /help при ошибке проверки
        logger.exception("Не удалось проверить права администратора для /help.")
    return HELP_MENU_TEXT


def _chat_id_for_link(chat_id: int) -> str:
    chat_id_str = str(chat_id)
    if chat_id_str.startswith("-100"):
        return chat_id_str[4:]
    if chat_id_str.startswith("-"):
        return chat_id_str[1:]
    return chat_id_str


def _topic_link(title: str, thread_id: int | None) -> str:
    if thread_id is None:
        return title
    chat_id_str = _chat_id_for_link(settings.forum_chat_id)
    return f'<a href="https://t.me/c/{chat_id_str}/{thread_id}">{title}</a>'


def _menu_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, topic in enumerate(TOPIC_ORDER, 1):
        row.append(
            InlineKeyboardButton(
                text=topic,
                callback_data=f"{CALLBACK_TOPIC}:{topic}",
            )
        )
        if index % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text="Куда писать?",
                callback_data=CALLBACK_WHERE,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=CALLBACK_BACK)]]
    )


def _classify_topic(text: str) -> str | None:
    best_topic: str | None = None
    best_score = 0
    for topic, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_topic = topic
    if best_score >= 1:
        return best_topic
    return None


def _state_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return (chat_id, user_id)


def _message_key(chat_id: int, message_id: int) -> tuple[int, int]:
    return (chat_id, message_id)


def _clear_waiting_state(key: tuple[int, int]) -> None:
    HELP_ROUTING_STATE.pop(key, None)
    task = HELP_TIMEOUT_TASKS.pop(key, None)
    if task:
        task.cancel()


def _clear_delete_task(key: tuple[int, int]) -> None:
    task = HELP_DELETE_TASKS.pop(key, None)
    if task:
        task.cancel()


async def _delete_help_message(bot: Bot, key: tuple[int, int]) -> None:
    await asyncio.sleep(HELP_DELETE_TIMEOUT.total_seconds())
    task_key = _message_key(*key)
    HELP_DELETE_TASKS.pop(task_key, None)
    try:
        await bot.delete_message(chat_id=key[0], message_id=key[1])
    except Exception:  # noqa: BLE001 - сообщение могло быть уже удалено
        return


def schedule_help_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    key = _message_key(chat_id, message_id)
    _clear_delete_task(key)
    HELP_DELETE_TASKS[key] = asyncio.create_task(_delete_help_message(bot, key))


async def _run_timeout(bot: Bot, key: tuple[int, int]) -> None:
    await asyncio.sleep(WAITING_TIMEOUT.total_seconds())
    state = HELP_ROUTING_STATE.get(key)
    if state is None:
        return
    now = datetime.now(timezone.utc)
    if now - state.started_at < WAITING_TIMEOUT:
        return
    _clear_waiting_state(key)
    await bot.edit_message_text(
        HELP_TIMEOUT_TEXT,
        chat_id=state.chat_id,
        message_id=state.message_id,
        reply_markup=_menu_keyboard(),
    )


def _ai_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return (chat_id, user_id)


def _get_ai_context(chat_id: int, user_id: int) -> list[str]:
    """Быстрый in-memory fallback — используется если БД-контекст не загружен."""
    history = AI_CHAT_HISTORY.get(_ai_key(chat_id, user_id))
    if history is None:
        return []
    return list(history)




def _remember_ai_exchange(chat_id: int, user_id: int, prompt: str, reply: str) -> None:
    """Сохраняет обмен в in-memory историю (обратная совместимость для тестов)."""
    history = AI_CHAT_HISTORY.setdefault(
        _ai_key(chat_id, user_id),
        deque(maxlen=AI_CHAT_HISTORY_LIMIT),
    )
    history.append(f"user: {prompt[:1000]}")
    history.append(f"assistant: {reply[:800]}")


async def _get_ai_context_persistent(chat_id: int, user_id: int) -> list[str]:
    """Загружает контекст из БД (персистентный) с fallback на in-memory."""
    try:
        async for session in get_session():
            ctx = await load_context(session, chat_id, user_id)
            if ctx:
                return ctx
    except Exception:
        logger.warning("Не удалось загрузить историю из БД, используем in-memory.")
    return _get_ai_context(chat_id, user_id)


async def _remember_ai_exchange_persistent(
    chat_id: int, user_id: int, prompt: str, reply: str
) -> None:
    """Сохраняет обмен в БД + in-memory кэш."""
    _remember_ai_exchange(chat_id, user_id, prompt, reply)

    # Персистентное сохранение в БД
    try:
        async for session in get_session():
            await save_exchange(session, chat_id, user_id, prompt, reply)
    except Exception:
        logger.warning("Не удалось сохранить историю диалога в БД.")

    # Проверяем, нужно ли сжатие (conversation summary)
    try:
        await _try_compress_history(chat_id, user_id)
    except Exception:
        logger.warning("Не удалось выполнить сжатие истории диалога.")


async def _try_compress_history(chat_id: int, user_id: int) -> None:
    """Сжимает старые сообщения в саммари через LLM, если порог достигнут."""
    async for session in get_session():
        messages = await get_messages_for_compression(session, chat_id, user_id)
        if messages is None:
            return

        # Формируем текст для сжатия
        text_to_summarize = "\n".join(f"{m.role}: {m.text}" for m in messages)
        old_ids = [m.id for m in messages]

        # Вызываем LLM для генерации саммари
        try:
            summary = await get_ai_client().summarize_conversation(
                text_to_summarize, chat_id=chat_id
            )
        except Exception:
            # Если LLM недоступен — делаем простое обрезание
            summary = "Ранее обсуждали: " + "; ".join(
                m.text[:80] for m in messages if m.role == "user"
            )[:500]

        await replace_with_summary(session, chat_id, user_id, old_ids, summary)


def _feedback_keyboard(bot_message_id: int) -> InlineKeyboardMarkup:
    """Создаёт inline-кнопки для оценки ответа ИИ."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="\U0001f44d",
            callback_data=f"{FEEDBACK_PREFIX}:up:{bot_message_id}",
        ),
        InlineKeyboardButton(
            text="\U0001f44e",
            callback_data=f"{FEEDBACK_PREFIX}:down:{bot_message_id}",
        ),
    ]])


def _extract_ai_prompt(message: Message) -> str:
    text = (_get_message_text(message) or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""

    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"^(бот|bot|помощник|ассистент)[,:\s-]*", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    return text[:1000]


def _is_ai_reply_rate_limited(chat_id: int, user_id: int) -> bool:
    key = _ai_key(chat_id, user_id)
    now = datetime.now(timezone.utc)
    last_reply = LAST_AI_REPLY_TIME.get(key)
    if last_reply and now - last_reply < AI_MENTION_COOLDOWN:
        return True
    LAST_AI_REPLY_TIME[key] = now
    return False


async def set_waiting_state(
    bot: Bot,
    chat_id: int,
    user_id: int,
    message_id: int,
    message_thread_id: int | None,
) -> None:
    key = _state_key(chat_id, user_id)
    _clear_waiting_state(key)
    HELP_ROUTING_STATE[key] = HelpRoutingState(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        message_thread_id=message_thread_id,
        started_at=datetime.now(timezone.utc),
    )
    HELP_TIMEOUT_TASKS[key] = asyncio.create_task(_run_timeout(bot, key))


def clear_routing_state(
    user_id: int | None = None,
    chat_id: int | None = None,
) -> int:
    if user_id is None and chat_id is None:
        keys = list(HELP_ROUTING_STATE.keys())
        for key in keys:
            _clear_waiting_state(key)
        return len(keys)

    keys = [
        key
        for key in HELP_ROUTING_STATE
        if (user_id is None or key[1] == user_id)
        and (chat_id is None or key[0] == chat_id)
    ]
    for key in keys:
        _clear_waiting_state(key)
    return len(keys)


@router.message(Command("start"))
@router.message(Command("help"))
async def help_command(message: Message, bot: Bot) -> None:
    logger.info("HANDLER: help_command")
    if message.chat.id != settings.forum_chat_id:
        await message.reply("Команда /help работает только в форуме ЖК.")
        return
    if message.from_user:
        key = _state_key(message.chat.id, message.from_user.id)
        _clear_waiting_state(key)
    menu_text = await _get_menu_text(bot, message.from_user.id if message.from_user else None)
    response = await message.answer(
        menu_text,
        reply_markup=_menu_keyboard(),
    )
    schedule_help_delete(message.bot, response.chat.id, response.message_id)
    logger.info("OUT: HELP_MENU")


def _get_message_text(message: Message) -> str | None:
    """Возвращает текст сообщения или подпись, если это медиа."""
    return message.text or message.caption


def _get_message_entities(message: Message) -> list[MessageEntity]:
    """Возвращает сущности сообщения или подписи."""
    return message.entities or message.caption_entities or []


def _is_bot_mentioned(message: Message, bot_user: object) -> bool:
    """Проверяет упоминание бота по сущностям и тексту."""
    text = _get_message_text(message)
    if text is None:
        return False
    username = getattr(bot_user, "username", None)
    bot_id = getattr(bot_user, "id", None)

    for entity in _get_message_entities(message):
        if entity.type == "text_mention" and getattr(entity, "user", None):
            if bot_id is not None and entity.user.id == bot_id:
                return True
        if entity.type == "mention" and username:
            mention = text[entity.offset:entity.offset + entity.length]
            if mention.lower() == f"@{username.lower()}":
                return True

    if username and f"@{username.lower()}" in text.lower():
        return True

    return False


def _is_bot_name_called(text: str | None, bot_user: object) -> bool:
    """Проверяет обращение к боту по имени без @."""
    if text is None:
        return False
    lowered = text.casefold()
    first_name = getattr(bot_user, "first_name", None)
    full_name = getattr(bot_user, "full_name", None)
    candidates = [name for name in (first_name, full_name) if name]
    for name in candidates:
        pattern = rf"(?<!\w){re.escape(str(name).casefold())}(?!\w)"
        if re.search(pattern, lowered):
            return True
    return False


@router.callback_query(F.data == CALLBACK_BACK)
async def help_back(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    key = _state_key(callback.message.chat.id, callback.from_user.id)
    _clear_waiting_state(key)
    menu_text = await _get_menu_text(callback.message.bot, callback.from_user.id)
    await callback.message.edit_text(
        menu_text,
        reply_markup=_menu_keyboard(),
    )
    schedule_help_delete(
        callback.message.bot,
        callback.message.chat.id,
        callback.message.message_id,
    )
    await callback.answer()


@router.callback_query(F.data == CALLBACK_WHERE)
async def help_where(callback: CallbackQuery, bot: Bot) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    key = _state_key(callback.message.chat.id, callback.from_user.id)
    now = datetime.now(timezone.utc)
    last_hint = LAST_HINT_TIME.get(key)
    if last_hint and now - last_hint < HINT_COOLDOWN:
        await callback.message.edit_text(
            HELP_RATE_LIMIT_TEXT,
            reply_markup=_back_keyboard(),
        )
        schedule_help_delete(
            callback.message.bot,
            callback.message.chat.id,
            callback.message.message_id,
        )
        await callback.answer()
        return
    await set_waiting_state(
        bot,
        callback.message.chat.id,
        callback.from_user.id,
        callback.message.message_id,
        callback.message.message_thread_id,
    )
    await callback.message.edit_text(
        HELP_WAIT_TEXT,
        reply_markup=_back_keyboard(),
    )
    schedule_help_delete(
        callback.message.bot,
        callback.message.chat.id,
        callback.message.message_id,
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{CALLBACK_TOPIC}:"))
async def help_topic(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    topic = callback.data.split(":", maxsplit=2)[-1] if callback.data else ""
    description = TOPIC_DESCRIPTIONS.get(topic)
    if description is None:
        await callback.answer()
        return
    key = _state_key(callback.message.chat.id, callback.from_user.id)
    _clear_waiting_state(key)
    thread_id = TOPIC_THREADS.get(topic)
    if thread_id is None:
        reply_text = description
    else:
        reply_text = (
            f"{description}\n\n"
            f"Перейти в тему: {_topic_link(topic, thread_id)}"
        )
    await callback.message.edit_text(
        reply_text,
        reply_markup=_back_keyboard(),
        parse_mode="HTML",
    )
    schedule_help_delete(
        callback.message.bot,
        callback.message.chat.id,
        callback.message.message_id,
    )
    await callback.answer()


@router.message(Command("ai"), flags={"block": False})
async def ai_command(message: Message) -> None:
    if message.chat.id not in _ASSISTANT_CHAT_IDS:
        await message.reply("Команда /ai работает только в форуме ЖК и чате логов.")
        return
    if message.from_user is None or message.from_user.is_bot:
        return
    prompt = _extract_ai_prompt(message)
    if not prompt:
        await message.reply("Напишите вопрос после команды: /ai <ваш вопрос>")
        return

    question_key = _normalize_cache_key(prompt)

    context = await _get_ai_context_persistent(message.chat.id, message.from_user.id)
    reply = await get_ai_client().assistant_reply(
        prompt, context, chat_id=message.chat.id,
    )

    await _remember_ai_exchange_persistent(
        message.chat.id, message.from_user.id, prompt, reply,
    )

    # Трекаем вопрос в FAQ
    await _track_faq(message.chat.id, question_key, reply)

    # Отправляем ответ с кнопками оценки
    sent = await message.reply(reply, reply_markup=_feedback_keyboard(0))
    # Обновляем кнопки с реальным message_id
    _PENDING_FEEDBACK[sent.message_id] = (
        message.chat.id, message.from_user.id, prompt[:1000], reply[:800], question_key,
    )
    try:
        await sent.edit_reply_markup(reply_markup=_feedback_keyboard(sent.message_id))
    except Exception:
        pass  # Не критично если не удалось обновить кнопки


@router.message(BotMentionFilter(), flags={"block": False})
async def mention_help(message: Message, bot: Bot) -> None:
    logger.info(f"HANDLER: mention_help called, text={message.text!r}")
    me = await _get_bot_profile(bot)
    username = getattr(me, "username", None)
    if username:
        logger.info(f"HANDLER: mention_help MATCH @{username}")
    else:
        logger.info("HANDLER: mention_help MATCH by id")
    if message.chat.id not in _ASSISTANT_CHAT_IDS:
        return
    if message.from_user is None:
        return

    # Проверяем модерацию перед ответом ассистента
    prompt = _extract_ai_prompt(message)
    if prompt:
        from app.handlers.moderation import run_moderation

        moderated = await run_moderation(message, bot)
        if moderated:
            return  # сообщение нарушает правила, ассистент не отвечает

    if _is_ai_reply_rate_limited(message.chat.id, message.from_user.id):
        logger.info("OUT: MENTION_REPLY_SKIPPED_RATE_LIMIT")
        await message.reply(AI_RATE_LIMIT_TEXT)
        return

    if prompt:
        question_key = _normalize_cache_key(prompt)

        context = await _get_ai_context_persistent(message.chat.id, message.from_user.id)
        reply = await get_ai_client().assistant_reply(prompt, context, chat_id=message.chat.id)

        await _remember_ai_exchange_persistent(
            message.chat.id, message.from_user.id, prompt, reply,
        )

        # Трекаем вопрос в FAQ
        await _track_faq(message.chat.id, question_key, reply)

        # Отправляем с кнопками оценки
        sent = await message.reply(reply, reply_markup=_feedback_keyboard(0))
        _PENDING_FEEDBACK[sent.message_id] = (
            message.chat.id, message.from_user.id, prompt[:1000], reply[:800], question_key,
        )
        try:
            await sent.edit_reply_markup(reply_markup=_feedback_keyboard(sent.message_id))
        except Exception:
            pass
    else:
        reply = _next_mention_reply()
        await message.reply(reply)
    logger.info("OUT: MENTION_REPLY")


@router.callback_query(F.data.startswith(f"{FEEDBACK_PREFIX}:"))
async def ai_feedback_callback(callback: CallbackQuery) -> None:
    """Обработчик кнопок оценки ответа ИИ."""
    if callback.data is None or callback.from_user is None or callback.message is None:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    direction = parts[1]  # up / down
    try:
        bot_msg_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    rating = 1 if direction == "up" else -1

    pending = _PENDING_FEEDBACK.get(bot_msg_id)
    if pending is None:
        await callback.answer("Оценка уже недоступна.")
        return

    chat_id, original_user_id, prompt_text, reply_text, question_key = pending

    # Сохраняем feedback в БД
    try:
        async for session in get_session():
            fb = await save_feedback(
                session,
                chat_id=chat_id,
                user_id=callback.from_user.id,
                bot_message_id=bot_msg_id,
                prompt_text=prompt_text,
                reply_text=reply_text,
                rating=rating,
            )
            if fb is None:
                await callback.answer("Вы уже оценивали этот ответ.")
                return

            # Обновляем рейтинг FAQ
            await update_faq_rating(
                session, chat_id=chat_id, question_key=question_key, delta=rating,
            )
            await session.commit()
            break
    except Exception:
        logger.warning("Не удалось сохранить feedback.")
        await callback.answer("Ошибка при сохранении оценки.")
        return

    # Убираем кнопки после оценки
    emoji = "\U0001f44d" if rating > 0 else "\U0001f44e"
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer(f"Спасибо за оценку {emoji}")

    # Очищаем кэш, чтобы не копился бесконечно
    if len(_PENDING_FEEDBACK) > 500:
        oldest_keys = sorted(_PENDING_FEEDBACK.keys())[:250]
        for k in oldest_keys:
            _PENDING_FEEDBACK.pop(k, None)


async def _check_faq(chat_id: int, question_key: str) -> str | None:
    """Проверяет FAQ на закреплённый ответ."""
    try:
        async for session in get_session():
            return await get_faq_answer(session, chat_id=chat_id, question_key=question_key)
    except Exception:
        logger.warning("Не удалось проверить FAQ.")
    return None


async def _track_faq(chat_id: int, question_key: str, answer: str) -> None:
    """Трекает вопрос в FAQ."""
    try:
        async for session in get_session():
            await track_question(session, chat_id=chat_id, question_key=question_key, answer=answer)
            await session.commit()
    except Exception:
        logger.warning("Не удалось обновить FAQ-трекинг.")


@router.message(HelpRoutingActiveFilter(), flags={"block": False})
async def help_routing_response(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return
    if message.chat.id != settings.forum_chat_id:
        return
    key = _state_key(message.chat.id, message.from_user.id)
    state = HELP_ROUTING_STATE.get(key)
    if state is None:
        return
    if message.message_thread_id != state.message_thread_id:
        return
    text = (_get_message_text(message) or "").strip()
    if not text or text.startswith("/"):
        return
    now = datetime.now(timezone.utc)
    if now - state.started_at >= WAITING_TIMEOUT:
        _clear_waiting_state(key)
        return
    topic = _classify_topic(text.lower())
    _clear_waiting_state(key)
    if topic is None:
        complaints_link = _topic_link("Жалобы", TOPIC_THREADS["Жалобы"])
        smoke_link = _topic_link("Курилке", TOPIC_THREADS["Курилка"])
        reply_text = (
            f"Не уверен, но можно в {complaints_link} "
            f"или задать вопрос в {smoke_link}."
        )
    else:
        thread_id = TOPIC_THREADS.get(topic)
        if thread_id is None:
            reply_text = f"Ваш вопрос подходит для темы «{topic}»."
        else:
            reply_text = f"Ваш вопрос подходит для темы {_topic_link(topic, thread_id)}."
    LAST_HINT_TIME[key] = now
    await bot.edit_message_text(
        reply_text,
        chat_id=state.chat_id,
        message_id=state.message_id,
        reply_markup=_back_keyboard(),
        parse_mode="HTML",
    )
    schedule_help_delete(bot, state.chat_id, state.message_id)
