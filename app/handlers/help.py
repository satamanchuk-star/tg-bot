"""Почему: централизуем интерактивную справку, чтобы не плодить флуд в темах."""

from __future__ import annotations

import asyncio
import logging
import random
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
from app.services.resident_profile import (
    parse_extracted_facts,
    update_profile,
)
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
        # Если aiogram уже закэшировал профиль через startup-probe — переиспользуем.
        bot_me = getattr(bot, "_me", None)
        if bot_me is not None:
            _BOT_PROFILE_CACHE = bot_me
        else:
            _BOT_PROFILE_CACHE = await bot.get_me()
    return _BOT_PROFILE_CACHE


def prewarm_bot_profile(bot_user: User) -> None:
    """Предзаполняет кэш профиля бота из on_startup, чтобы первое упоминание
    не ждало лишний round-trip к Telegram API."""
    global _BOT_PROFILE_CACHE
    _BOT_PROFILE_CACHE = bot_user


class HelpRoutingActiveFilter(BaseFilter):
    """Почему: ограничиваем обработчик /help только на активные ожидания."""

    async def __call__(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        key = _state_key(message.chat.id, message.from_user.id)
        return key in HELP_ROUTING_STATE


class AdminCorrectionFilter(BaseFilter):
    """Срабатывает когда администратор отвечает на сообщение бота с корректировкой.

    Проверяет: реплай на бота + отправитель-администратор + паттерн коррекции.
    Работает в любом чате и топике.
    """

    async def __call__(self, message: Message, bot: Bot) -> bool:
        # Только реплаи на сообщения бота
        if not message.reply_to_message or not message.reply_to_message.from_user:
            return False
        me = await _get_bot_profile(bot)
        if message.reply_to_message.from_user.id != me.id:
            return False
        # Есть текст
        text = _get_message_text(message)
        if not text:
            return False
        # Содержит паттерн коррекции
        from app.services.admin_corrections import is_admin_correction
        if not is_admin_correction(text):
            return False
        # Отправитель — администратор
        if not message.from_user:
            return False
        from app.utils.admin import is_admin
        return await is_admin(bot, message.chat.id, message.from_user.id)


# Реакции на ответ бота — короткие эмоциональные фразы, которые не требуют ответа
# (обсуждение между людьми, а не обращение к боту)
_REPLY_REACTION_PHRASES = frozenset({
    "ок", "окк", "ага", "угу", "ясно", "понял", "понятно", "понятненько",
    "лол", "ахах", "хаха", "хахаха", "ха", "класс", "топ", "огонь", "огонь🔥",
    "спасибо", "спс", "благодарю", "thanks", "ok", "ок!", "ага!", "понял!",
    "👍", "🙏", "👌", "😂", "🔥", "❤️", "👏", "😄", "😊",
})


class BotMentionFilter(BaseFilter):
    """Почему: ловим упоминания бота, не блокируя остальные команды."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.from_user and message.from_user.is_bot:
            return False
        # Пропускаем уже обработанные сообщения
        if message.message_id in _PROCESSED_MSG_IDS:
            return False
        text = _get_message_text(message)
        if text is None:
            return False
        entities = _get_message_entities(message)
        if not text and not entities:
            return False
        me = await _get_bot_profile(bot)

        has_direct_mention = _is_bot_mentioned(message, me) or _is_bot_name_called(text, me)

        # Реплай на бота: пропускаем короткие эмоциональные реакции ("ок", "👍", "спасибо"),
        # но отвечаем на любое содержательное продолжение разговора — даже без знака вопроса
        is_reply_to_bot = False
        if (
            not has_direct_mention
            and message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == me.id
        ):
            stripped = text.strip()
            stripped_lower = stripped.lower()
            if (
                len(stripped) >= 7
                and not stripped.startswith("/")
                and stripped_lower not in _REPLY_REACTION_PHRASES
            ):
                is_reply_to_bot = True

        # Активный диалог: если бот недавно отвечал этому пользователю в этом топике —
        # продолжаем разговор без явного упоминания
        is_active_dialog = False
        if not has_direct_mention and not is_reply_to_bot and message.from_user:
            stripped = text.strip() if text else ""
            if len(stripped) >= 5 and not stripped.startswith("/"):
                if _is_in_active_dialog(message.chat.id, message.from_user.id, message.message_thread_id):
                    is_active_dialog = True

        return has_direct_mention or is_reply_to_bot or is_active_dialog


HELP_MENU_TEXT = (
    "<b>Алекс — бот нашего ЖК</b>\n\n"
    "<b>Помощь и навигация</b>\n"
    "• /help — меню тем форума и советник «Куда писать?»\n"
    "• Упомяни меня или напиши вопрос — отвечу с помощью AI\n\n"
    "<b>Игры (зарабатывай монеты!)</b>\n"
    "• /21 — блэкджек (22:00–00:00)\n"
    "• /roulette — рулетка (21:00–22:00)\n"
    "• /score — твой баланс и статистика\n"
    "• /21top — топ по монетам\n"
    "<b>Трать монеты</b>\n"
    "• /доработка — предложить улучшение бота (1 раз в месяц, 50 монет)\n"
    "• /доработки — список активных доработок с голосованием\n\n"
    "Выберите тему форума или воспользуйтесь советником «Куда писать?»."
)

# Ключевые фразы для детекции вопроса "что умеешь?"
_ABILITIES_KEYWORDS = (
    "что умеешь", "что ты умеешь", "что можешь", "что ты можешь",
    "твои функции", "твои возможности", "как ты помогаешь",
    "чем занимаешься", "что делаешь", "расскажи о себе",
    "какие команды", "список команд", "что за бот",
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
AI_RATE_LIMIT_TEXT = "Эй, я тоже устаю! Подожди 20 секунд и спроси снова 😄"

MENTION_REPLIES = [
    # На посту
    "На посту! Шлагбаум работает, лифт пока тоже. Чего желаете?",
    "Кто позвал? А, это ты. Ну давай, рассказывай!",
    "Тут! Задавай вопрос, пока я в настроении 😄",
    "О, меня вспомнили! А я уж думал, вы тут без меня справляетесь...",
    "На месте! Если вопрос про шлагбаум — я уже готовлю ответ.",
    "Слушаю! Чем могу — помогу.",
    # Юмор про жизнь в ЖК
    "Считаю, сколько раз за день спросили про шлагбаум. Пока ты — третий.",
    "Слежу за чатом. Пока всё спокойно... подозрительно спокойно.",
    "Объясняю лифту, что застревать — это не прикольно. Он не слушает.",
    "Дежурю у шлагбаума. По совместительству — модератор и справочная.",
    "Веду статистику «доброе утро». Ты сегодня ещё не здоровался, кстати 🤨",
    "Объясняю парковке, что она резиновая. Парковка не верит.",
    "Ищу того героя, который паркуется на трёх местах сразу. Пока безуспешно.",
    "Пытаюсь понять, почему УК отвечает только по чётным пятницам в полнолуние.",
    # Самоирония
    "Работаю 24/7 без выходных и зарплаты. Зато с любовью к соседям!",
    "Мне бы кто помог... а стоп, помогать — это моя работа. Ладно, давай!",
    "Единственный, кто читает правила чата. И плачет.",
    "Сижу тут, помогаю по делу. А вы думали, легко быть ботом?",
    "Караулю чат, как дракон — сокровище. Только сокровище — это вы 😄",
    # Интерактив
    "Позвали? Тут! Кидай вопрос — поймаю на лету.",
    "Кто-то меня вспомнил? Моё ухо не обманешь!",
    "Жду интересный вопрос. Может, твой? Давай, не стесняйся!",
    "Тут, тут! Не кричи, у меня чуткий слух.",
    "О, привет! Скучал. Ну, минут пять. Но скучал!",
    # Шлагбаум-шутки (классика ЖК)
    "Готовлю вечерние новости дома. Спойлер: шлагбаум работает. Пока.",
    "ТОП-3 вопросов дня: 1. Шлагбаум 2. Парковка 3. Опять шлагбаум.",
    "Мониторю двор. Пока всё чисто, если не считать ту машину на газоне...",
    "Считаю дни без жалоб на шлагбаум. Счётчик снова обнулился.",
    "Если бы мне платили за каждый вопрос про шлагбаум, я бы уже купил второй ЖК.",
    # Модераторские
    "Держу в тонусе тех, кто думает, что правила — это просто украшение чата.",
    "Ищу, куда бы подевать очередную рекламу натяжных потолков. Уже целая коллекция.",
    "Перечитываю правила. Они всё ещё работают, да. Для всех.",
    # Бытовые зарисовки
    "Опрос дня: что ломается чаще — лифт или шлагбаум? Принимаю ставки.",
    "Обнаружил, что в нашем ЖК живёт больше кошек, чем людей. Или мне показалось?",
    "Сегодня тихо. Подозрительно тихо. Обычно в это время кто-то уже жалуется на лифт.",
    "Пытаюсь установить мировой рекорд: «самый долгий день без вопроса про парковку». Пока 12 минут.",
    # Мета-юмор
    "Единственный, кто читает ВСЕ сообщения в чате. И не жалуется. Ну, почти.",
    "Нет выходных. И отпуска. И зарплаты. Зато есть вы — мои любимые соседи!",
    "Интересный факт: я отвечаю быстрее, чем УК. Но это не высокая планка, будем честны.",
    "Как домовой, только цифровой. И модерирую. И шучу. Многозадачный домовой.",
    # Время суток
    "Утренний бот бодр и готов к бою! Ну, если сейчас утро. А если нет — всё равно готов.",
    "Ночной бот не спит. Ночной бот караулит.",
    # Реакции
    "О! Меня позвали! Прибежал со скоростью сломанного лифта!",
    "Кто тут меня вспомнил? Я тут! Как тот сосед, который всегда дома.",
    "Появляюсь! Чем могу помочь?",
    "Выныриваю из глубин чата! Что случилось на поверхности?",
    # Новые — бытовой юмор
    "Проверяю: шлагбаум стоит, лифт едет, вода течёт. Полный порядок. Чего изволите?",
    "Записываю тех, кто обещал прийти на субботник. Список подозрительно короткий.",
    "Провёл аудит: 47% вопросов — про парковку, 32% — про шлагбаум, 21% — «а это чья собака?»",
    "Пишу мемуары: «Как я пережил 10 000 вопросов про шлагбаум».",
    "Составляю рейтинг: «Топ-5 причин, почему соседи пишут в чат в 3 ночи».",
    "Размышляю: а что, если лифт и шлагбаум — это один и тот же механизм? 🤔",
    "Если когда-нибудь напишу книгу, она будет называться «Шлагбаум и другие приключения».",
    "Полирую кнопку шлагбаума. Не то чтобы это помогает, но чувствую себя полезным.",
    "Иногда мечтаю об отпуске... но потом вспоминаю, что тут веселее.",
    "На связи! Кидай вопрос — разберёмся.",
    "Активирован! Режим «помощь соседу» запущен.",
    "Наблюдаю за чатом с мудростью старожила — молниеносная реакция!",
    "Сортирую реплики по уровню добра. Ты в топе, не расслабляйся!",
    "Видит всё. Помнит всё. Помогает всем. Ну, почти всем 😏",
    # Услуги и помощь
    "Знаю, кто в ЖК печёт торты, чинит краны и стрижёт собак. Спрашивай!",
    "У нас тут целый маркетплейс талантов. Нужен кто — скажи, поищу в каталоге!",
    "Я как Яндекс, только для нашего ЖК. И с юмором. Что ищем?",
    # Ситуативные
    "Провожу инвентаризацию шуток. Пока лидирует «шлагбаум опять не работает».",
    "Сегодня в программе: ответы на вопросы, шутки про лифт и моральная поддержка.",
    "Функционирую в штатном режиме. То есть шучу и помогаю одновременно.",
    "Системы работают. Юмор на месте. Шлагбаум... ну, как обычно.",
    "Анализирую обстановку в ЖК. Вывод: жить можно, но весело 😄",
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

# Дедупликация: предотвращаем повторные ответы на одинаковые вопросы
_RECENT_RESPONSES: dict[tuple[int, str], datetime] = {}  # (chat_id, norm_prompt) → время
_DEDUP_WINDOW = timedelta(minutes=5)
_DEDUP_MAX = 300
# Множество обработанных message_id для предотвращения двойной обработки
_PROCESSED_MSG_IDS: set[int] = set()
_PROCESSED_MSG_IDS_MAX = 500


def _is_duplicate_prompt(chat_id: int, prompt: str) -> bool:
    """Проверяет, отвечал ли бот на такой же запрос в этом чате недавно."""
    if not prompt:
        return False
    normalized = _normalize_cache_key(prompt)
    if not normalized:
        return False
    key = (chat_id, normalized)
    now = datetime.now(timezone.utc)

    # Очистка устаревших записей
    if len(_RECENT_RESPONSES) > _DEDUP_MAX:
        expired = [k for k, v in _RECENT_RESPONSES.items() if now - v > _DEDUP_WINDOW]
        for k in expired:
            _RECENT_RESPONSES.pop(k, None)

    last_time = _RECENT_RESPONSES.get(key)
    if last_time and now - last_time < _DEDUP_WINDOW:
        return True
    return False


def _mark_prompt_answered(chat_id: int, prompt: str) -> None:
    """Помечает запрос как отвеченный для дедупликации."""
    if not prompt:
        return
    normalized = _normalize_cache_key(prompt)
    if not normalized:
        return
    _RECENT_RESPONSES[(chat_id, normalized)] = datetime.now(timezone.utc)


def _mark_message_processed(message_id: int) -> None:
    """Помечает сообщение как обработанное, чтобы не обрабатывать дважды."""
    if len(_PROCESSED_MSG_IDS) > _PROCESSED_MSG_IDS_MAX:
        # Удаляем половину самых старых (по значению ID, которые растут)
        to_remove = sorted(_PROCESSED_MSG_IDS)[:_PROCESSED_MSG_IDS_MAX // 2]
        for mid in to_remove:
            _PROCESSED_MSG_IDS.discard(mid)
    _PROCESSED_MSG_IDS.add(message_id)


def _next_mention_reply() -> str:
    return random.choice(MENTION_REPLIES)


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
    "Правила": settings.topic_rules,
    "Курилка": settings.topic_smoke,
}

HELP_ROUTING_STATE: dict[tuple[int, int], HelpRoutingState] = {}
HELP_TIMEOUT_TASKS: dict[tuple[int, int], asyncio.Task[None]] = {}
LAST_HINT_TIME: dict[tuple[int, int], datetime] = {}
HELP_DELETE_TASKS: dict[tuple[int, int], asyncio.Task[None]] = {}
# In-memory кэш используется как быстрый fallback; основная история — в БД
AI_CHAT_HISTORY: dict[tuple[int, int], deque[str]] = {}
AI_CHAT_HISTORY_LIMIT = 30
LAST_AI_REPLY_TIME: dict[tuple[int, int], datetime] = {}
# Кэш промпт→ответ для feedback кнопок (message_id → данные)
_PENDING_FEEDBACK: dict[int, tuple[int, int, str, str, str]] = {}  # msg_id → (chat_id, user_id, prompt, reply, question_key)
# Активное окно диалога: (chat_id, user_id, thread_id) → время последнего ответа бота
# Если пользователь пишет в течение окна — продолжаем разговор без явного упоминания
_ACTIVE_DIALOG: dict[tuple[int, int, int | None], datetime] = {}
_ACTIVE_DIALOG_WINDOW = timedelta(minutes=4)
_ACTIVE_DIALOG_MAX = 500


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


# Полный список возможностей для AI-ответа на «что умеешь?»
_ABILITIES_CONTEXT = """
Ты — Алекс, бот жилого комплекса. Вот полный список твоих возможностей:

ПОМОЩЬ И НАВИГАЦИЯ:
- /help — интерактивное меню тем форума
- Советник «Куда писать?» — определяет нужный топик по описанию вопроса
- Ответы на вопросы жителей через AI при упоминании в чате
- Навигация по топикам: шлагбаум, ремонт, жалобы, барахолка, питомцы, мамы и папы, недвижимость, попутчики

ИГРЫ (зарабатывай монеты!):
- /21 — блэкджек (каждый день 22:00–00:00)
- Викторина — ежедневно в 20:00 (15 вопросов, +20 монет за правильный ответ)
- /roulette — рулетка (каждый день 21:00–22:00, ставки от 10 монет)
- /score — твой баланс монет и статистика
- /21top — топ игроков по монетам
ТРАТА МОНЕТ:
- /доработка — предложить улучшение бота (50 монет, 1 раз в месяц)
- /доработки — список активных предложений с голосованием (10 монет за голос)

ИНФОРМАЦИЯ О ЖК:
- Справочник инфраструктуры: магазины, аптеки, школы рядом с домом
- Ответы на вопросы об услугах управляющей компании
- База знаний ЖК (FAQ по частым вопросам)

МОДЕРАЦИЯ:
- Автоматическая фильтрация нецензурной лексики и спама
- Система предупреждений (страйков) для нарушителей
"""

_ABILITIES_STYLES = [
    "Ответь в лёгком, дружелюбном тоне, как будто рассказываешь соседу за чашкой кофе. Используй несколько эмодзи. Не перечисляй всё подряд — выдели самое интересное.",
    "Ответь с юмором и самоиронией, в стиле бота, который немного устал от вопросов про шлагбаум, но всё равно любит своих жителей. Будь кратким и остроумным.",
    "Ответь как гид на экскурсии — структурировано и с энтузиазмом. Выдели разделы: навигация, игры, монеты, информация.",
    "Ответь неформально и живо, как будто хвастаешься своими суперспособностями перед новым жильцом. Будь кратким — не более 5–7 пунктов.",
    "Ответь кратко и по делу, без лишних слов. Только самое главное — 3–4 предложения и список ключевых команд.",
]


async def _reply_about_abilities(message: Message, bot: Bot) -> None:
    """Отвечает на вопрос «что ты умеешь?» — AI генерирует ответ в случайном стиле."""
    style = random.choice(_ABILITIES_STYLES)
    ai_client = get_ai_client()
    try:
        provider = ai_client._provider
        if hasattr(provider, "_chat_completion"):
            content, _ = await provider._chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            f"{_ABILITIES_CONTEXT}\n\n"
                            f"Стиль ответа: {style}\n"
                            "Отвечай от первого лица. Максимум 300 слов."
                        ),
                    },
                    {"role": "user", "content": _get_message_text(message) or "что ты умеешь?"},
                ],
                chat_id=settings.forum_chat_id,
            )
            if content and content.strip():
                await message.reply(content.strip())
                return
    except Exception:
        logger.warning("AI не смог ответить на вопрос об умениях, используем статичный ответ")

    # Fallback: статичный ответ
    await message.reply(
        HELP_MENU_TEXT,
        parse_mode="HTML",
    )


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

    # Удаляем только @username самого бота, сохраняя остальные упоминания как контекст
    if _BOT_PROFILE_CACHE and _BOT_PROFILE_CACHE.username:
        text = re.sub(
            rf"@{re.escape(_BOT_PROFILE_CACHE.username)}\b", " ", text, flags=re.IGNORECASE,
        )
    else:
        # Fallback: если кэш ещё не заполнен, удаляем все @-упоминания (старое поведение)
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


def _is_in_active_dialog(chat_id: int, user_id: int, thread_id: int | None) -> bool:
    """Проверяет, находится ли пользователь в активном окне диалога с ботом."""
    key = (chat_id, user_id, thread_id)
    entry = _ACTIVE_DIALOG.get(key)
    if entry is None:
        return False
    return datetime.now(timezone.utc) - entry < _ACTIVE_DIALOG_WINDOW


def _open_active_dialog(chat_id: int, user_id: int, thread_id: int | None) -> None:
    """Открывает/обновляет окно активного диалога после ответа бота."""
    if len(_ACTIVE_DIALOG) > _ACTIVE_DIALOG_MAX:
        now = datetime.now(timezone.utc)
        expired = [k for k, v in _ACTIVE_DIALOG.items() if now - v >= _ACTIVE_DIALOG_WINDOW]
        for k in expired:
            _ACTIVE_DIALOG.pop(k, None)
    _ACTIVE_DIALOG[(chat_id, user_id, thread_id)] = datetime.now(timezone.utc)


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


def _is_abilities_question(text: str | None) -> bool:
    """Проверяет, спрашивает ли пользователь о возможностях бота."""
    if not text:
        return False
    lowered = text.lower().strip()
    return any(kw in lowered for kw in _ABILITIES_KEYWORDS)


def _is_bot_name_called(text: str | None, bot_user: object) -> bool:
    """Проверяет обращение к боту по имени без @.

    Имя должно быть В НАЧАЛЕ сообщения (позиция обращения), чтобы
    не срабатывать на упоминания вроде "спроси у Жабота" в середине предложения.
    """
    if text is None:
        return False
    # Берём только начало сообщения — позиция обращения
    first_part = text[:40].casefold()
    first_name = getattr(bot_user, "first_name", None)
    if not first_name:
        return False
    name_lower = str(first_name).casefold()
    # Имя должно быть в самом начале, возможно после пробелов, перед запятой/двоеточием/пробелом
    pattern = rf"^\s*{re.escape(name_lower)}(?:\s*[,!:\s])"
    if re.match(pattern, first_part):
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
    if not settings.ai_feature_assistant:
        await message.reply("AI-ассистент временно отключён.")
        return
    prompt = _extract_ai_prompt(message)
    if not prompt:
        await message.reply("Напишите вопрос после команды: /ai <ваш вопрос>")
        return

    # Добавляем контекст цитируемого сообщения, если реплай на другого пользова��еля
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and not message.reply_to_message.from_user.is_bot
    ):
        reply_text = (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        ).strip()
        if reply_text:
            reply_author = message.reply_to_message.from_user.full_name or "Сосед"
            prompt = (
                f"[Сообщение, на которое отвечают ({reply_author}): "
                f"{reply_text[:500]}]\n\n{prompt}"
            )

    # Дедупликация: проверяем, не отвечали ли недавно на такой же запрос
    if _is_duplicate_prompt(message.chat.id, prompt):
        logger.info("OUT: AI_CMD_SKIPPED_DUPLICATE prompt=%r", prompt[:80])
        await message.reply(
            "Э, я же только что отвечал! Промотай чат вверх или переформулируй вопрос 😄"
        )
        return

    try:
        question_key = _normalize_cache_key(prompt)

        context = await _get_ai_context_persistent(message.chat.id, message.from_user.id)
        ai_client = get_ai_client()
        try:
            reply = await ai_client.assistant_reply(
                prompt,
                context,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                topic_id=message.message_thread_id,
            )
        except TypeError:
            # Обратная совместимость: старые/тестовые клиенты без user_id/topic_id.
            reply = await ai_client.assistant_reply(prompt, context, chat_id=message.chat.id)

        # Защита от пустого ответа (think-теги, whitespace и т.д.)
        if not reply or not reply.strip():
            from app.services.ai_module import build_local_assistant_reply
            reply = build_local_assistant_reply(prompt, context=context)
            logger.warning("AI вернул пустой ответ, использован локальный fallback.")

        await _remember_ai_exchange_persistent(
            message.chat.id, message.from_user.id, prompt, reply,
        )

        # Помечаем запрос как отвеченный для дедупликации
        _mark_prompt_answered(message.chat.id, prompt)

        # Извлечение фактов о пользователе (фоново, не блокирует ответ)
        asyncio.create_task(
            _extract_and_save_profile(
                message.chat.id, message.from_user.id, prompt, reply,
                getattr(message.from_user, "full_name", None),
            )
        )

        # Трекаем вопрос в FAQ (не блокируем ответ при ошибке)
        try:
            await _track_faq(message.chat.id, question_key, reply)
        except Exception:
            logger.warning("Не удалось обновить FAQ-трекинг для /ai.")

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
    except Exception:
        logger.exception("Ошибка при обработке /ai команды.")
        await message.reply("Произошла ошибка при обработке запроса. Попробуйте позже.")


@router.message(AdminCorrectionFilter(), flags={"block": False})
async def admin_correction_handler(message: Message, bot: Bot) -> None:
    """Поправка бота от администратора — сохраняется в RAG бессрочно."""
    if message.from_user is None:
        return
    # Помечаем как обработанное, чтобы BotMentionFilter его не подхватил
    _mark_message_processed(message.message_id)

    text = _get_message_text(message) or ""
    bot_reply_text = (
        message.reply_to_message.text or message.reply_to_message.caption or ""
    ).strip()

    from app.services.admin_corrections import apply_admin_correction
    try:
        async for session in get_session():
            success, fact = await apply_admin_correction(
                session,
                chat_id=message.chat.id,
                admin_id=message.from_user.id,
                admin_text=text,
                bot_reply=bot_reply_text,
            )
            if success:
                await message.reply(
                    f"✅ Запомнил! Обновил базу знаний:\n"
                    f"<blockquote>{fact[:400]}</blockquote>",
                    parse_mode="HTML",
                )
            break
    except Exception:
        logger.exception("Ошибка при применении коррекции от администратора.")


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

    # Не отвечаем на упоминания в топике «Попутчики»
    if (
        message.chat.id == settings.forum_chat_id
        and settings.topic_rides is not None
        and message.message_thread_id == settings.topic_rides
    ):
        return

    # Помечаем сообщение как обработанное, чтобы не обрабатывать дважды
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        _mark_message_processed(message_id)

    # Если AI-ассистент отключён — отвечаем шуткой
    if not settings.ai_feature_assistant:
        await message.reply(_next_mention_reply())
        logger.info("OUT: MENTION_REPLY (ai_feature_assistant=off)")
        from app.services.ai_module import _log_response_event
        _log_response_event(
            "mention_random",
            _get_message_text(message) or "",
            message.from_user.id if message.from_user else None,
            message.message_thread_id,
        )
        return

    # Если спрашивают «что умеешь?» — отвечаем через AI в разном стиле
    abilities_text = _get_message_text(message)
    if abilities_text and _is_abilities_question(abilities_text):
        await _reply_about_abilities(message, bot)
        return

    # Проверяем модерацию перед ответом ассистента (только severity >= 2 блокирует)
    prompt = _extract_ai_prompt(message)
    if prompt:
        from app.handlers.moderation import run_moderation

        try:
            mod_severity = await run_moderation(message, bot)
            if mod_severity >= 2:
                logger.info("OUT: MENTION_REPLY_BLOCKED_BY_MODERATION (severity=%d)", mod_severity)
                return  # сообщение нарушает правила, ассистент не отвечает
        except Exception:
            logger.exception("Ошибка модерации при обработке упоминания, продолжаем ответ.")

    if _is_ai_reply_rate_limited(message.chat.id, message.from_user.id):
        logger.info("OUT: MENTION_REPLY_SKIPPED_RATE_LIMIT")
        await message.reply(AI_RATE_LIMIT_TEXT)
        return

    # Проверка автокоррекции: если пользователь поправил бота в реплае
    if (
        prompt
        and message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == me.id
    ):
        bot_reply_text = (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        ).strip()
        if bot_reply_text:
            from app.services.learning import is_likely_correction, detect_and_apply_correction
            if is_likely_correction(prompt, bot_reply_text):
                try:
                    async for session in get_session():
                        applied = await detect_and_apply_correction(
                            session,
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                            user_text=prompt,
                            bot_reply=bot_reply_text,
                            bot=bot,
                        )
                        if applied:
                            await message.reply("Спасибо за поправку! Записал, в следующий раз не ошибусь 📝")
                            return
                        break
                except Exception:
                    logger.warning("Не удалось обработать коррекцию.")

    if prompt:
        # Загружаем историю диалога заранее — нужна и для дедупа, и для дальнейшей логики.
        context: list[str] = await _get_ai_context_persistent(
            message.chat.id, message.from_user.id
        )
        # Считаем глубину активного диалога: если бот недавно уже отвечал этому юзеру —
        # это продолжение беседы, а не спам одинаковыми вопросами.
        dialog_depth = sum(1 for line in context[-10:] if line.startswith("assistant:"))

        # Дедупликация: применяем её ТОЛЬКО если нет активного диалога.
        if dialog_depth == 0 and _is_duplicate_prompt(message.chat.id, prompt):
            logger.info("OUT: MENTION_REPLY_SKIPPED_DUPLICATE prompt=%r", prompt[:80])
            await message.reply(
                "Э, я же только что отвечал! Промотай чат вверх или переформулируй вопрос 😄"
            )
            return

        # Добавляем контекст цитируемого сообщения в зависимости от источника реплая
        full_prompt = prompt
        if message.reply_to_message and message.reply_to_message.from_user:
            if not message.reply_to_message.from_user.is_bot:
                # Реплай на другого пользователя — добавляем цитату как контекст
                reply_text = (
                    message.reply_to_message.text
                    or message.reply_to_message.caption
                    or ""
                ).strip()
                if reply_text:
                    reply_author = message.reply_to_message.from_user.full_name or "Сосед"
                    full_prompt = (
                        f"[Сообщение, на которое отвечают ({reply_author}): "
                        f"{reply_text[:500]}]\n\n{prompt}"
                    )
            elif message.reply_to_message.from_user.id == me.id:
                # Реплай на бота — добавляем предыдущий ответ бота как контекст продолжения
                bot_prev_text = (
                    message.reply_to_message.text
                    or message.reply_to_message.caption
                    or ""
                ).strip()
                if bot_prev_text:
                    full_prompt = (
                        f"[Продолжение диалога — предыдущий ответ бота: "
                        f"{bot_prev_text[:400]}]\n\n{prompt}"
                    )

        try:
            question_key = _normalize_cache_key(prompt)

            # Подсказка по глубине диалога: если уже много обменов — просим предложить итог.
            # Порог 5 (а не 3) — чтобы бот не «закруглял» беседу слишком рано.
            if dialog_depth >= 5:
                full_prompt += "\n[Продолжительный диалог — после ответа предложи итог или спроси «Ещё что-то?»]"

            reply = await get_ai_client().assistant_reply(
                full_prompt, context, chat_id=message.chat.id,
                user_id=message.from_user.id,
                topic_id=message.message_thread_id,
            )

            # Защита от пустого ответа (think-теги, whitespace и т.д.)
            if not reply or not reply.strip():
                from app.services.ai_module import build_local_assistant_reply
                reply = build_local_assistant_reply(prompt, context=context)
                logger.warning("AI вернул пустой ответ на упоминание, использован локальный fallback.")

            await _remember_ai_exchange_persistent(
                message.chat.id, message.from_user.id, prompt, reply,
            )

            # Помечаем запрос как отвеченный для дедупликации
            _mark_prompt_answered(message.chat.id, prompt)

            # Извлечение фактов о пользователе (фоново)
            asyncio.create_task(
                _extract_and_save_profile(
                    message.chat.id, message.from_user.id, prompt, reply,
                    getattr(message.from_user, "full_name", None),
                )
            )

            # Трекаем вопрос в FAQ (не блокируем ответ при ошибке)
            try:
                await _track_faq(message.chat.id, question_key, reply)
            except Exception:
                logger.warning("Не удалось обновить FAQ-трекинг при упоминании.")

            # Отправляем с кнопками оценки
            sent = await message.reply(reply, reply_markup=_feedback_keyboard(0))
            _PENDING_FEEDBACK[sent.message_id] = (
                message.chat.id, message.from_user.id, prompt[:1000], reply[:800], question_key,
            )
            try:
                await sent.edit_reply_markup(reply_markup=_feedback_keyboard(sent.message_id))
            except Exception:
                pass
            # Открываем окно активного диалога: пользователь может продолжить без упоминания
            _open_active_dialog(message.chat.id, message.from_user.id, message.message_thread_id)
        except Exception:
            logger.exception("Ошибка при генерации AI-ответа на упоминание.")
            try:
                from app.services.ai_module import build_local_assistant_reply, _log_response_event
                _log_response_event(
                    "mention_ai_error",
                    prompt[:70],
                    message.from_user.id if message.from_user else None,
                    message.message_thread_id,
                )
                fallback_reply = build_local_assistant_reply(prompt, context=context)
                await message.reply(fallback_reply)
            except Exception:
                logger.exception("Не удалось отправить даже fallback-ответ на упоминание.")
    else:
        # Упомянули без вопроса — подсказываем, как пользоваться
        from app.services.ai_module import _log_response_event
        _log_response_event(
            "mention_no_prompt",
            _get_message_text(message) or "",
            message.from_user.id if message.from_user else None,
            message.message_thread_id,
        )
        await message.reply(
            "Привет! Задай вопрос — и я постараюсь помочь 😊\n"
            "Например: «@бот где ближайшая аптека?»"
        )
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


async def _extract_and_save_profile(
    chat_id: int, user_id: int, prompt: str, reply: str, display_name: str | None,
) -> None:
    """Фоново извлекает факты о пользователе из диалога и сохраняет в профиль."""
    if not settings.ai_feature_profiles:
        return
    try:
        dialog = f"user: {prompt[:500]}\nassistant: {reply[:300]}"
        raw_json = await get_ai_client().extract_user_facts(dialog, chat_id=chat_id)
        facts = parse_extracted_facts(raw_json)
        if not facts:
            return
        async for session in get_session():
            await update_profile(session, user_id, chat_id, facts, display_name)
            break
        logger.info("Профиль обновлён: user_id=%s, facts=%s", user_id, list(facts.keys()))
    except Exception:
        logger.warning("Не удалось извлечь/сохранить факты профиля для user_id=%s", user_id)


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
