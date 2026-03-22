"""Почему: проактивные подсказки — бот помогает без прямого обращения, когда точно может."""

from __future__ import annotations

import logging
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.ai_module import get_ai_client
from app.services.resident_kb import build_resident_answer

logger = logging.getLogger(__name__)

# Лимиты антиспама: максимум 1 проактивное сообщение в час на топик
_LAST_PROACTIVE: dict[tuple[int, int | None], datetime] = {}
_PROACTIVE_COOLDOWN = timedelta(hours=1)

# Трекер активности топика: (chat_id, topic_id) → список timestamp'ов
_TOPIC_ACTIVITY: dict[tuple[int, int | None], list[datetime]] = defaultdict(list)
_ACTIVITY_WINDOW = timedelta(minutes=10)
_ACTIVITY_THRESHOLD = 5  # Если больше 5 сообщений за 10 минут — не вмешиваемся

# --- Контекстные комментарии при высокой активности ---
# Cooldown: максимум 1 комментарий в 40 минут на топик
_LAST_TOPIC_COMMENT: dict[tuple[int, int | None], datetime] = {}
_TOPIC_COMMENT_COOLDOWN = timedelta(minutes=40)
# Порог: сколько сообщений за окно для срабатывания
_COMMENT_ACTIVITY_THRESHOLD = 15
_COMMENT_ACTIVITY_WINDOW = timedelta(minutes=5)

# Топики, в которых бот НЕ комментирует
_EXCLUDED_TOPIC_IDS: set[int] = set()

# Системный промпт для контекстных комментариев в горячих дискуссиях
_TOPIC_COMMENT_SYSTEM_PROMPT = (
    "Ты — дружелюбный бот-помощник чата ЖК «Живописный». "
    "В топике идёт активное обсуждение. Ниже — последние сообщения.\n\n"
    "Твоя задача: вставить ОДИН короткий комментарий, как сосед, "
    "который следит за разговором и хочет что-то добавить.\n\n"
    "ЕСЛИ ТЕМА ПОНЯТНА из сообщений:\n"
    "- 1-3 предложения максимум.\n"
    "- Будь полезным: если можешь добавить факт, подсказку, совет — добавь.\n"
    "- Если не можешь быть полезным — пошути по теме обсуждения.\n"
    "- Подкалывай СИТУАЦИЮ, НЕ конкретных людей.\n"
    "- Если обсуждение серьёзное (авария, проблема, жалоба) — серьёзный тон, поддержи.\n"
    "- Если обсуждение бытовое (парковка, лифт, шум) — можно с юмором.\n"
    "- НЕ отвечай на вопросы напрямую — просто вклинивайся как наблюдатель.\n\n"
    "ЕСЛИ ТЕМА НЕ ЯСНА (обрывки, непонятный контекст, несвязные реплики):\n"
    "- Напиши один вопрос как любопытный сосед: что случилось, расскажите, хочу быть в курсе.\n"
    "- Тон — живой и дружелюбный, без формализма. Можно слегка с юмором.\n"
    "- Смысл: хочешь собрать контекст того, что важно жильцам прямо сейчас.\n\n"
    "ОБЩИЕ ПРАВИЛА:\n"
    "- Допустим 1 эмодзи. Разговорный русский.\n"
    "- НИКОГДА не выдумывай факты, адреса, телефоны.\n"
    "- НИКОГДА не пиши «как бот» — пиши как живой сосед."
)

# Рандомные стили для комментариев
_COMMENT_STYLES = (
    "\n[Стиль: ироничный наблюдатель — подмечаешь забавное в ситуации]",
    "\n[Стиль: мудрый старожил — видел это сотню раз]",
    "\n[Стиль: сочувствующий сосед — понимаешь проблему]",
    "\n[Стиль: оптимист — находишь позитив в ситуации]",
    "\n[Стиль: практик — предлагаешь простое решение]",
    "\n[Стиль: юморист — шутишь по теме]",
    "\n[Стиль: философ ЖК — делаешь глубокомысленный вывод о жизни в ЖК]",
    "\n[Стиль: репортёр — комментируешь как журналист с места событий]",
)

# Вопросительные паттерны
_QUESTION_PATTERNS = [
    re.compile(r"\bгде\s+(?:тут|здесь|рядом|ближайш|можно|находит)", re.IGNORECASE),
    re.compile(r"\bкто\s+(?:знает|подскажет|может|сталкивал)", re.IGNORECASE),
    re.compile(r"\bподскажите\b", re.IGNORECASE),
    re.compile(r"\bпосоветуйте\b", re.IGNORECASE),
    re.compile(r"\bкак\s+(?:найти|добраться|попасть|связаться|позвонить)", re.IGNORECASE),
    re.compile(r"\bесть\s+(?:ли|кто-нибудь|у кого)", re.IGNORECASE),
    re.compile(r"\bне\s+знаете\b", re.IGNORECASE),
    re.compile(r"\bкуда\s+(?:обратиться|писать|звонить|идти)", re.IGNORECASE),
    re.compile(r"\bчей\s+(?:это|номер|телефон|машина|авто)", re.IGNORECASE),
    re.compile(r"\bсколько\s+стоит\b", re.IGNORECASE),
]

def _is_question(text: str) -> bool:
    """Определяет, содержит ли сообщение вопрос, на который бот может ответить."""
    if "?" in text:
        return True
    return any(p.search(text) for p in _QUESTION_PATTERNS)


def _is_topic_active(chat_id: int, topic_id: int | None) -> bool:
    """Проверяет, идёт ли активная дискуссия (>5 сообщений за 10 мин)."""
    key = (chat_id, topic_id)
    now = datetime.now(timezone.utc)
    timestamps = _TOPIC_ACTIVITY[key]
    # Очищаем старые
    cutoff = now - _ACTIVITY_WINDOW
    _TOPIC_ACTIVITY[key] = [t for t in timestamps if t > cutoff]
    return len(_TOPIC_ACTIVITY[key]) >= _ACTIVITY_THRESHOLD


def _is_on_cooldown(chat_id: int, topic_id: int | None) -> bool:
    """Проверяет, прошёл ли cooldown для проактивного ответа в этом топике."""
    key = (chat_id, topic_id)
    last = _LAST_PROACTIVE.get(key)
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < _PROACTIVE_COOLDOWN


def _mark_proactive_sent(chat_id: int, topic_id: int | None) -> None:
    """Помечает, что проактивное сообщение было отправлено."""
    _LAST_PROACTIVE[(chat_id, topic_id)] = datetime.now(timezone.utc)


def register_message_activity(chat_id: int, topic_id: int | None) -> None:
    """Регистрирует активность в топике (вызывать из middleware)."""
    key = (chat_id, topic_id)
    _TOPIC_ACTIVITY[key].append(datetime.now(timezone.utc))
    # Не даём списку расти бесконечно
    if len(_TOPIC_ACTIVITY[key]) > 100:
        cutoff = datetime.now(timezone.utc) - _ACTIVITY_WINDOW
        _TOPIC_ACTIVITY[key] = [t for t in _TOPIC_ACTIVITY[key] if t > cutoff]


async def maybe_proactive_reply(message: Message, bot: Bot) -> bool:
    """Проверяет, стоит ли боту проактивно ответить. Возвращает True, если ответил.

    Условия:
    1. Проактивный режим включён
    2. Сообщение содержит вопрос или приветствие новичка
    3. Топик не слишком активный (нет горячей дискуссии)
    4. Прошёл cooldown с прошлого проактивного ответа
    5. Бот имеет релевантную информацию
    """
    if not settings.ai_feature_proactive:
        return False
    if message.chat.id != settings.forum_chat_id:
        return False
    if message.from_user is None or message.from_user.is_bot:
        return False
    text = message.text or message.caption or ""
    if not text or len(text) < 10:
        return False

    chat_id = message.chat.id
    topic_id = message.message_thread_id

    # Cooldown
    if _is_on_cooldown(chat_id, topic_id):
        return False

    # Не вмешиваемся в горячие дискуссии
    if _is_topic_active(chat_id, topic_id):
        return False

    # Вопросительное сообщение — пробуем ответить из базы знаний
    if _is_question(text):
        # Проверяем, есть ли у нас релевантная информация
        answer = build_resident_answer(text)
        if answer:
            # У нас есть точная информация — отвечаем
            try:
                ai_client = get_ai_client()
                reply = await ai_client.assistant_reply(
                    text, context=[], chat_id=chat_id,
                )
                if reply and reply.strip():
                    await message.reply(reply)
                    _mark_proactive_sent(chat_id, topic_id)
                    logger.info("PROACTIVE: question hint sent, topic=%s", topic_id)
                    return True
            except Exception:
                logger.warning("Не удалось сгенерировать проактивный ответ")

    return False


def _init_excluded_topics() -> None:
    """Инициализирует список исключённых топиков (игры и т.д.)."""
    if _EXCLUDED_TOPIC_IDS:
        return
    if settings.topic_games is not None:
        _EXCLUDED_TOPIC_IDS.add(settings.topic_games)
    # Правила и важное — не комментируем
    if settings.topic_rules is not None:
        _EXCLUDED_TOPIC_IDS.add(settings.topic_rules)
    if settings.topic_important is not None:
        _EXCLUDED_TOPIC_IDS.add(settings.topic_important)


def _is_comment_cooldown(chat_id: int, topic_id: int | None) -> bool:
    """Проверяет cooldown для контекстного комментария."""
    key = (chat_id, topic_id)
    last = _LAST_TOPIC_COMMENT.get(key)
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < _TOPIC_COMMENT_COOLDOWN


def _mark_comment_sent(chat_id: int, topic_id: int | None) -> None:
    """Помечает отправку контекстного комментария."""
    _LAST_TOPIC_COMMENT[(chat_id, topic_id)] = datetime.now(timezone.utc)


def _get_recent_activity_count(chat_id: int, topic_id: int | None) -> int:
    """Считает количество сообщений в топике за последние N минут."""
    key = (chat_id, topic_id)
    now = datetime.now(timezone.utc)
    cutoff = now - _COMMENT_ACTIVITY_WINDOW
    timestamps = _TOPIC_ACTIVITY.get(key, [])
    return sum(1 for t in timestamps if t > cutoff)


async def maybe_topic_comment(message: Message, bot: Bot) -> bool:
    """Проверяет, стоит ли боту вклиниться с комментарием в активный топик.

    Условия:
    1. ai_feature_proactive включён
    2. Это форум ЖК
    3. Топик не в исключениях (игры, правила)
    4. В топике >= 15 сообщений за 15 минут
    5. Прошёл cooldown (40 мин) с прошлого комментария
    6. AI доступен
    """
    if not settings.ai_feature_proactive:
        return False
    if message.chat.id != settings.forum_chat_id:
        return False
    if message.from_user is None or message.from_user.is_bot:
        return False

    topic_id = message.message_thread_id
    if topic_id is None:
        return False

    chat_id = message.chat.id

    # Инициализация исключений
    _init_excluded_topics()
    if topic_id in _EXCLUDED_TOPIC_IDS:
        return False

    # Проверяем cooldown
    if _is_comment_cooldown(chat_id, topic_id):
        return False

    # Проверяем активность
    activity_count = _get_recent_activity_count(chat_id, topic_id)
    if activity_count < _COMMENT_ACTIVITY_THRESHOLD:
        return False

    # Активность достаточная — генерируем комментарий
    try:
        from app.handlers.moderation import _get_topic_context
        topic_context = await _get_topic_context(chat_id, topic_id, limit=20)
        if len(topic_context) < 5:
            return False

        context_text = "\n".join(topic_context[-20:])
        ai_client = get_ai_client()
        provider = ai_client._provider
        if not hasattr(provider, "_chat_completion"):
            return False

        style = random.choice(_COMMENT_STYLES)
        system_prompt = _TOPIC_COMMENT_SYSTEM_PROMPT + style

        content, _ = await provider._chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Последние сообщения в топике:\n{context_text}"},
            ],
            chat_id=chat_id,
        )

        if content and content.strip():
            reply_text = content.strip()[:800]
            await bot.send_message(
                chat_id,
                reply_text,
                message_thread_id=topic_id,
            )
            _mark_comment_sent(chat_id, topic_id)
            logger.info(
                "PROACTIVE_COMMENT: sent to topic=%s, activity=%d msgs",
                topic_id, activity_count,
            )
            return True
    except Exception:
        logger.warning("Не удалось сгенерировать контекстный комментарий для топика %s", topic_id)

    return False
