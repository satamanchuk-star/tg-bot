"""Почему: централизуем интерактивную справку, чтобы не плодить флуд в темах."""

from __future__ import annotations

import asyncio
import logging
import random
import re
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
)

from app.config import settings
from app.utils.admin import is_admin
from app.utils.admin_help import ADMIN_HELP

logger = logging.getLogger(__name__)
router = Router()


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
        me = await bot.get_me()
        return _is_bot_mentioned(message, me) or _is_bot_name_called(text, me)


HELP_MENU_TEXT = (
    "Я подсказываю, где обсуждать вопросы, и отвечаю на упоминания. "
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

MENTION_REPLIES = [
    "Я тут, на посту! Проверяю, чтобы котики не получили бан по ошибке.",
    "Шлифую правила, полирую шлагбаум — всё под контролем!",
    "Считаю монеты, чтобы не убежали из банка.",
    "Охочусь на флуд. Пока что флуд прячется!",
    "Нагреваю викторину. Вопросы уже на взлёте.",
    "Тестирую шутки. Эта прошла контроль качества.",
    "Слежу, чтобы объявления не убежали в оффтоп.",
    "Делаю вид, что отдыхаю. На самом деле модерирую.",
    "Полирую игровые карты. Блэкджек ждёт!",
    "Проверяю, кто забыл сказать «доброе утро».",
    "Сканирую чат на предмет мемов. Всё стабильно.",
    "Отвечаю на упоминания. Это моя суперсила.",
    "Поднимаю щит модерации, но улыбаюсь по-дружески.",
    "Проверяю, чтобы соседям было уютно, как в тапочках.",
    "Сверяю расписание викторин. Всё по секундам!",
    "Разгоняю пыль в чате, чтобы было чисто и весело.",
    "Взвешиваю монеты на улыбках — баланс идеален.",
    "Ищу потерянные мемы. Если найду — не отдам.",
    "Дежурю у шлагбаума, но по совместительству комик.",
    "Контролирую очередность тем. Порядок — моё второе имя.",
    "Пишу заметки о хорошем настроении. Записал твоё.",
    "Разминаю алгоритмы, чтобы отвечать быстрее.",
    "Собираю вопросы в викторину, как пазл на скорость.",
    "Приглядываю за чатом, как кот за окном.",
    "Строю мосты между темами, чтобы никто не потерялся.",
    "Охраняю тишину в ночи, чтобы всем сладко спалось.",
    "Сортирую реплики по уровню улыбок. Ты в топе.",
]

CALLBACK_PREFIX = "help"
CALLBACK_BACK = f"{CALLBACK_PREFIX}:back"
CALLBACK_WHERE = f"{CALLBACK_PREFIX}:where"
CALLBACK_TOPIC = f"{CALLBACK_PREFIX}:topic"

WAITING_TIMEOUT = timedelta(minutes=2)
HINT_COOLDOWN = timedelta(seconds=30)
HELP_DELETE_TIMEOUT = timedelta(minutes=2)


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
        parse_mode="HTML",
    )


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
    if message.message_thread_id is None:
        await message.reply("Команда /help работает внутри тем форума.")
        return
    if message.from_user:
        key = _state_key(message.chat.id, message.from_user.id)
        _clear_waiting_state(key)
    menu_text = await _get_menu_text(bot, message.from_user.id if message.from_user else None)
    response = await message.answer(
        menu_text,
        reply_markup=_menu_keyboard(),
        parse_mode="HTML",
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
        pattern = rf"(?<!\\w){re.escape(str(name).casefold())}(?!\\w)"
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
        parse_mode="HTML",
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
            parse_mode="HTML",
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
        parse_mode="HTML",
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


@router.message(BotMentionFilter(), flags={"block": False})
async def mention_help(message: Message, bot: Bot) -> None:
    logger.info(f"HANDLER: mention_help called, text={message.text!r}")
    me = await bot.get_me()
    username = getattr(me, "username", None)
    if username:
        logger.info(f"HANDLER: mention_help MATCH @{username}")
    else:
        logger.info("HANDLER: mention_help MATCH by id")
    await message.reply(random.choice(MENTION_REPLIES))
    logger.info("OUT: MENTION_REPLY")


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

