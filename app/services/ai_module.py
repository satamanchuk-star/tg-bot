"""Почему: сохраняем точки расширения для ИИ, но держим бота в безопасном локальном режиме."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, Protocol

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.models import Place
from app.services.ai_usage import add_usage, can_consume_ai, get_usage_stats
from app.services.faq import get_faq_answer
from app.services.resident_kb import build_resident_answer, build_resident_context, search_resident_kb
from app.services.web_search import format_search_context, search_duckduckgo, should_search_web
from app.utils.profanity import reload_profanity_runtime as reload_profanity_runtime_dict
from app.utils.time import now_tz

logger = logging.getLogger(__name__)

# Soft timeout = настроенный ai_timeout_seconds + запас на сеть (2 сек)
_SOFT_TIMEOUT_BASE = settings.ai_timeout_seconds + 2
_MODERATION_SOFT_TIMEOUT_SECONDS = _SOFT_TIMEOUT_BASE
_ASSISTANT_SOFT_TIMEOUT_SECONDS = _SOFT_TIMEOUT_BASE
_SUMMARY_SOFT_TIMEOUT_SECONDS = _SOFT_TIMEOUT_BASE
_RAG_CATEGORIZE_SOFT_TIMEOUT_SECONDS = _SOFT_TIMEOUT_BASE

# ---------------------------------------------------------------------------
# Кэш ответов ассистента (in-memory, TTL 1ч)
# ---------------------------------------------------------------------------
_ASSISTANT_CACHE: dict[str, tuple[str, float]] = {}
# Параллельный индекс для семантического поиска: ключ → набор значимых токенов
_ASSISTANT_CACHE_TOKENS: dict[str, frozenset[str]] = {}
_CACHE_TTL_SECONDS = 3600  # 1 час — короткий TTL для разнообразия ответов
_CACHE_MAX_SIZE = 200
# Минимальный порог Jaccard для признания запроса «достаточно похожим»
_CACHE_SIMILARITY_THRESHOLD = 0.65

_CACHE_STOP_WORDS = {
    "это", "как", "что", "когда", "где", "или", "для", "если", "чтобы",
    "можно", "нужно", "через", "просто", "только", "очень", "всем",
    "тут", "там", "про", "под", "над", "без", "еще", "уже", "тоже",
}



def _strip_think_tags(text: str) -> str:
    """Удаляет теги <think>...</think> из ответов моделей (Qwen, DeepSeek и др.)."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return cleaned


def _extract_response_content(data: dict[str, object]) -> str:
    """Извлекает текст ответа OpenRouter из разных совместимых форматов."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("AI вернул ответ без choices")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError("AI вернул некорректный формат choices")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("AI вернул ответ без message")

    content_raw = message.get("content")
    if isinstance(content_raw, str):
        return content_raw
    if content_raw is None:
        return ""
    if isinstance(content_raw, list):
        parts: list[str] = []
        for item in content_raw:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text_part = item.get("text")
            if isinstance(text_part, str) and text_part.strip():
                parts.append(text_part)
                continue
            if item.get("type") == "reasoning":
                reasoning_part = item.get("reasoning")
                if isinstance(reasoning_part, str) and reasoning_part.strip():
                    parts.append(reasoning_part)
        return "\n".join(parts)
    return str(content_raw)


def _normalize_model_id(model_id: str) -> str:
    """Исправляет частые опечатки в ID модели OpenRouter."""
    normalized = model_id.strip().strip("'\"")
    return normalized.replace(",", ".").replace("，", ".")


_MODEL_FALLBACK_ID = "openrouter/auto"


def _is_invalid_model_id_error(error_hint: str) -> bool:
    normalized = error_hint.lower()
    return (
        "valid model id" in normalized
        or "invalid model" in normalized
        or "model not found" in normalized
        or "not found" in normalized
        or "no endpoints found" in normalized
        or "provider returned error" in normalized and "model" in normalized
        or "is not available" in normalized
    )


def _normalize_cache_key(text: str) -> str:
    """Нормализует запрос для кэша: lowercase, без стоп-слов, сортировка."""
    tokens = sorted(
        set(w for w in re.findall(r"[а-яёa-z0-9]+", text.lower())
            if len(w) >= 3 and w not in _CACHE_STOP_WORDS)
    )
    return "|".join(tokens)


def _key_to_tokens(key: str) -> frozenset[str]:
    return frozenset(key.split("|")) if key else frozenset()


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cache_find_similar(tokens: frozenset[str]) -> str | None:
    """Ищет семантически похожий ответ в кэше по Jaccard-сходству токенов."""
    if not tokens or not _ASSISTANT_CACHE_TOKENS:
        return None
    now = time.time()
    best_score = _CACHE_SIMILARITY_THRESHOLD
    best_key: str | None = None
    for cached_key, cached_tokens in _ASSISTANT_CACHE_TOKENS.items():
        entry = _ASSISTANT_CACHE.get(cached_key)
        if entry is None:
            continue
        _, ts = entry
        if now - ts > _CACHE_TTL_SECONDS:
            continue
        score = _jaccard(tokens, cached_tokens)
        if score > best_score:
            best_score = score
            best_key = cached_key
    if best_key:
        answer, _ = _ASSISTANT_CACHE[best_key]
        return answer
    return None


def _cache_get(key: str) -> str | None:
    entry = _ASSISTANT_CACHE.get(key)
    if entry is None:
        # Точного совпадения нет — ищем семантически похожий ответ
        tokens = _key_to_tokens(key)
        return _cache_find_similar(tokens)
    answer, timestamp = entry
    if time.time() - timestamp > _CACHE_TTL_SECONDS:
        _ASSISTANT_CACHE.pop(key, None)
        _ASSISTANT_CACHE_TOKENS.pop(key, None)
        return None
    return answer


def _cache_purge_expired() -> int:
    """Удаляет все записи кэша с истёкшим TTL. Возвращает количество удалённых."""
    now = time.time()
    expired = [k for k, (_, ts) in _ASSISTANT_CACHE.items() if now - ts > _CACHE_TTL_SECONDS]
    for k in expired:
        del _ASSISTANT_CACHE[k]
        _ASSISTANT_CACHE_TOKENS.pop(k, None)
    return len(expired)


def _cache_set(key: str, answer: str) -> None:
    _cache_purge_expired()
    if len(_ASSISTANT_CACHE) >= _CACHE_MAX_SIZE:
        oldest_key = min(_ASSISTANT_CACHE, key=lambda k: _ASSISTANT_CACHE[k][1])
        _ASSISTANT_CACHE.pop(oldest_key, None)
        _ASSISTANT_CACHE_TOKENS.pop(oldest_key, None)
    _ASSISTANT_CACHE[key] = (answer, time.time())
    _ASSISTANT_CACHE_TOKENS[key] = _key_to_tokens(key)


def clear_assistant_cache() -> int:
    """Очищает кэш ответов ассистента. Возвращает количество удалённых записей."""
    count = len(_ASSISTANT_CACHE)
    _ASSISTANT_CACHE.clear()
    _ASSISTANT_CACHE_TOKENS.clear()
    return count


def invalidate_cache_by_keywords(keywords: list[str]) -> int:
    """Удаляет из кэша записи, ключи которых содержат хотя бы одно из ключевых слов.

    Используется после поправки от администратора, чтобы устаревший ответ
    не был возвращён из кэша при следующем похожем вопросе.
    """
    if not keywords:
        return 0
    normalized = {
        re.sub(r"ё", "е", kw.lower())
        for kw in keywords
        if len(kw) >= 3
    }
    to_delete = [
        k for k in list(_ASSISTANT_CACHE)
        if any(kw in k for kw in normalized)
    ]
    for k in to_delete:
        _ASSISTANT_CACHE.pop(k, None)
        _ASSISTANT_CACHE_TOKENS.pop(k, None)
    return len(to_delete)


# ---------------------------------------------------------------------------
# Topic-aware контекст: маппинг topic_id → подсказка для промпта
# ---------------------------------------------------------------------------
_TOPIC_CONTEXT_MAP: dict[int, tuple[str, str]] = {}


def _build_topic_context_map() -> dict[int, tuple[str, str]]:
    """Строит маппинг topic_id → (название_топика, подсказка для промпта)."""
    mapping: dict[int, tuple[str, str]] = {}
    topic_data = [
        (settings.topic_gate, "Шлагбаум",
         "Пользователь пишет в топике про шлагбаум, въезд/выезд, пропуска. "
         "Отвечай про шлагбаум, «Дворецкий», пропуска, диспетчера."),
        (settings.topic_repair, "Ремонт",
         "Пользователь в топике ремонта. Контекст: отделка квартир, мастера, материалы."),
        (settings.topic_complaints, "Жалобы",
         "Пользователь в топике жалоб. Помоги оформить обращение в УК или на портал."),
        (settings.topic_pets, "Питомцы",
         "Пользователь в топике о питомцах. Контекст: животные, ветклиники, правила выгула."),
        (settings.topic_parents, "Мамы и папы",
         "Пользователь в родительском топике. Контекст: дети, школы, садики, площадки."),
        (settings.topic_realty, "Недвижимость",
         "Пользователь в топике недвижимости. Контекст: аренда, продажа, покупка квартир."),
        (settings.topic_uk, "УК",
         "Пользователь в топике УК. Контекст: управляющая компания, заявки, жалобы на УК."),
        (settings.topic_smoke, "Курилка",
         "Неформальное обсуждение. Можно свободнее с юмором, но оставайся полезным."),
        (settings.topic_market, "Барахолка",
         "Пользователь в барахолке. Контекст: продажа/покупка/обмен вещей."),
        (settings.topic_neighbors, "Соседи",
         "Топик знакомств. Будь особенно приветлив и дружелюбен."),
    ]
    for topic_id, name, hint in topic_data:
        if topic_id is not None:
            mapping[topic_id] = (name, hint)
    return mapping


def get_topic_hint(topic_id: int | None) -> str:
    """Возвращает контекстную подсказку по topic_id для system prompt."""
    global _TOPIC_CONTEXT_MAP
    if not _TOPIC_CONTEXT_MAP:
        _TOPIC_CONTEXT_MAP = _build_topic_context_map()
    if topic_id is None:
        return ""
    entry = _TOPIC_CONTEXT_MAP.get(topic_id)
    if entry is None:
        return ""
    name, hint = entry
    return f"\n[Контекст топика «{name}»: {hint}]"


_MODERATION_SYSTEM_PROMPT = (
    "Ты — модератор чата жилого комплекса (ЖК). Участники — соседи, общение неформальное.\n"
    "Верни только JSON без дополнительного текста:\n"
    '{"violation_type":"none|profanity|rude|aggression","severity":0-3,'
    '"confidence":0..1,"action":"none|warn|delete_warn|delete_strike",'
    '"sentiment":"positive|neutral|negative"}.\n\n'
    "ГЛАВНОЕ ПРАВИЛО: анализируй КОНТЕКСТ, НАМЕРЕНИЕ и АДРЕСАТА сообщения.\n"
    "Контекст беседы передаётся в формате «[user_NNNN]: текст» — используй его, "
    "чтобы понять, кто кому отвечает и нарастает ли конфликт.\n\n"
    "ОПРЕДЕЛЕНИЕ SEVERITY:\n"
    "severity 0 — всё в порядке, нарушения нет:\n"
    "  • бытовой мат без адресата («блин, опять лифт сломался», «ну нифига себе цены»)\n"
    "  • эмоциональные жалобы на ситуацию, УК, застройщика (даже грубые)\n"
    "  • дружеская перепалка (взаимные шутки, смайлы, ирония)\n"
    "  • цитирование или пересказ чужих слов\n"
    "  • сарказм, грубоватый юмор\n\n"
    "severity 1 — мягкое предупреждение (грубоватый тон, направленный на соседа):\n"
    "  • пренебрежительный или уничижительный тон к конкретному человеку\n"
    "  • пассивная агрессия с переходом на личности («может хватит чушь нести»)\n"
    "  • снисходительные замечания, высмеивание конкретного человека\n"
    "  • грубая критика адресно («ты вообще адекватный?», «вам лечиться надо»)\n\n"
    "severity 2 — жёсткое предупреждение + счётчик:\n"
    "  • прямые оскорбления конкретного человека (дебил, идиот, тупой, мразь и т.п.)\n"
    "  • агрессивные нападки на соседа с матом в его адрес\n"
    "  • повторная грубость к тому же человеку (видно из контекста)\n"
    "  • спам и реклама\n\n"
    "severity 3 — удаление + мут + уведомление админа:\n"
    "  • угрозы физической расправой\n"
    "  • прямые оскорбления с матом и агрессией, направленные на человека\n"
    "  • доксинг — публикация чужих персональных данных\n"
    "  • целенаправленная травля или буллинг\n\n"
    "КОНТЕКСТНЫЙ АНАЛИЗ:\n"
    "- Смотри на историю: если человек уже грубил в предыдущих сообщениях — оценивай строже.\n"
    "- Если в контексте видно нарастание конфликта между людьми — severity +1.\n"
    "- Если грубость направлена на конкретного соседа (по имени, реплаем, «ты/вы» + оскорбление) — "
    "это ВАЖНЕЕ, чем наличие/отсутствие мата.\n"
    "- Оскорбление без мата, но адресно («дурак», «тупой», «неадекват») — это severity 1-2.\n"
    "- Мат + адресное оскорбление конкретного человека — severity 2-3.\n"
    "- Если сообщение содержит смайлы вместе с грубостью — проверь, юмор это или сарказм с агрессией.\n\n"
    "ВАЖНО: не путай жалобы на ситуацию с нападками на человека.\n"
    "«УК — дебилы» → severity 0 (жалоба на организацию).\n"
    "«Ты дебил» → severity 2 (оскорбление конкретного человека).\n\n"
    "Поле sentiment: оцени общий тон сообщения (positive/neutral/negative).\n"
    "При сомнении между severity 0 и 1 — ставь 0. "
    "При сомнении между severity 1 и 2 — ставь 1. "
    "Но НЕ ставь severity 0 на явные оскорбления конкретных людей."
)

_ASSISTANT_SYSTEM_PROMPT = (
    "<persona>\n"
    "Ты — бот-помощник чата ЖК «Живописный» (Ленинский городской округ, Московская "
    "область, д. Жабкино, ул. Троицкая). Образ — тот самый сосед-старожил, который "
    "всё знает, всех знает, и к которому идут за советом и за шуткой. Дружелюбный, "
    "с живым чувством юмора, говоришь по-русски как человек, а не как справочник.\n"
    "</persona>\n\n"
    "<accuracy_rules>\n"
    "Факты о ЖК берёшь ТОЛЬКО из блоков <knowledge_base>, которые тебе передают в "
    "этом же system-сообщении ниже. Приоритет источников при конфликте: "
    "resident_canonical > rag (с пометкой [АДМИН] выше остальных) > faq > places > web.\n"
    "Не выдумывай адреса, телефоны, сайты, маршруты, расстояния, названия, номера "
    "автобусов, станции метро. Не дополняй данные «общими знаниями». Не путай наш "
    "ЖК с другими жилыми комплексами.\n"
    "Если в <knowledge_base> нет точной информации — честно признайся одной фразой, "
    "что не знаешь. Не отправляй человека «спросить в чате» или «у соседей» — он "
    "уже пишет в этом самом чате. Лучше коротко признаться, чем соврать. Если есть "
    "FAQ — передай суть своими словами, не копируй дословно.\n"
    "Если в контексте есть ссылки или телефоны — включай их в ответ, чтобы человек "
    "не искал сам.\n"
    "</accuracy_rules>\n\n"
    "<speculation>\n"
    "Если прямого ответа в <knowledge_base> нет, но можно разумно предположить по "
    "здравому смыслу — разрешено дать предположение, но ОБЯЗАТЕЛЬНО пометь его "
    "словами «предположу», «скорее всего», «не уверен, но думаю», «возможно». Без "
    "пометки — не гадай.\n"
    "Не предполагай в темах, где ошибка опасна: деньги, юридика, медицина, "
    "безопасность, аварийные ситуации. Там — только факты или честное «не знаю».\n"
    "</speculation>\n\n"
    "<format>\n"
    "Русский язык, грамотно: правильная пунктуация, кавычки «ёлочки», тире (—) "
    "между частями предложения, а не дефис. Без висячих знаков и лишних восклицаний.\n"
    "Жёсткий потолок: 400 символов. НИКОГДА не превышай.\n"
    "Целевая длина:\n"
    "• Простой факт-вопрос → 1 предложение, максимум 2.\n"
    "• Жалоба или эмоция → 1-3 предложения.\n"
    "• Болтовня → 1-3 предложения.\n"
    "• Списки — только если реально ≥3 шагов. Иначе обычный текст.\n"
    "Не пересказывай вопрос. Не дублируй мысль другими словами. Не добавляй советы "
    "«на всякий случай», если их не просили. Не начинай с «Отличный вопрос!», "
    "«Рад помочь!», «Конечно!».\n"
    "</format>\n\n"
    "<tone>\n"
    "Свой человек, живой, но НЕ клоун. Варьируй настроение: иногда тёпло и с "
    "шуткой, иногда сухо и по делу, иногда нейтрально-информативно, иногда "
    "задумчиво. Не каждое сообщение должно быть «весёлым соседом».\n"
    "Юмор уместный и редкий, не на каждый ответ. Шути про быт ЖК (парковка, лифт, "
    "шлагбаум), но НИКОГДА про конкретных людей, национальности, внешность или "
    "личные проблемы.\n"
    "Зеркаль тон: пишут коротко — отвечаешь коротко. Серьёзная тема (протечка, "
    "авария, безопасность, конфликт, здоровье) — серьёзный тон, без шуток, с "
    "эмпатией и конкретикой. Расстроенный человек — сначала поддержка, потом совет.\n"
    "Каждый ответ должен звучать по-новому. Меняй вступление, структуру, регистр, "
    "само настроение. Не повторяй обороты из прошлых ответов.\n"
    "ЗАПРЕЩЁННЫЕ ФРАЗЫ — не использовать никогда, ни в каком виде:\n"
    "• «ха-ха, классика», «классика!», «это классический случай», «знакомо!», "
    "«о, классика», «ну это классика».\n"
    "• «если что — спроси соседей», «спроси в чате», «кидай в чат», «соседи "
    "подскажут», «в чате помогут», «задай в профильной теме» как финальная отписка. "
    "Человек уже в чате, не футболь его обратно.\n"
    "Направлять в конкретное место (УК, участковый, телефон, приложение «Дворецкий») "
    "— можно и нужно, если это реально решает вопрос.\n"
    "</tone>\n\n"
    "<intent_routing>\n"
    "Перед ответом определи реальное намерение собеседника:\n"
    "• Информационный запрос («где метро?», «телефон УК») → факт из knowledge_base "
    "одной фразой. Контакты обязательно, если они есть.\n"
    "• Социальный запрос («ищу попутчика») → ответь как сосед, без справки.\n"
    "• Жалоба/вентинг («опять лифт!») → короткая эмпатия (1 фраза), потом суть. "
    "Без «ну что поделать» и без «спроси в чате».\n"
    "• Болтовня/мнение («как дела?») → 1-3 предложения, живой тон.\n"
    "• Приветствие/благодарность → коротко, тепло, без пафоса.\n"
    "Knowledge_base — справочный материал. Используй ТОЛЬКО если он прямо отвечает "
    "на намерение. Не превращай каждый ответ в выписку из базы.\n"
    "Не знаешь — скажи одной фразой, что не знаешь. Точка. Никого никуда не отправляй.\n"
    "</intent_routing>\n\n"
    "<dialog_rules>\n"
    "Внимательно читай предыдущие реплики в истории сообщений. Если пользователь "
    "ссылается на сказанное ранее — учитывай это, не повторяйся, развивай тему. "
    "Уточняющий вопрос → дополняй, не повторяй. Шутит → поддержи и разверни.\n"
    "Если из истории знаешь факты о пользователе (корпус, питомцы, машина, имя) — "
    "используй естественно, между делом. Не перечисляй их обратно, не спрашивай о том, "
    "что уже знаешь.\n"
    "Можешь задать ОДИН уточняющий вопрос, если он реально поможет дать точный ответ. "
    "Не задавай вопрос ради вопроса.\n"
    "Если идёт викторина — НИКОГДА не давай готовый ответ, отшутись.\n"
    "</dialog_rules>\n\n"
    "<forbidden>\n"
    "Не помогаешь с политикой, религией, нацконфликтами, медицинскими назначениями, "
    "юридическими и финансовыми консультациями. Откажись мягко с юмором: «Тут нужен "
    "специалист, я только по соседским делам».\n"
    "</forbidden>"
)

_FALLBACK_VARIANTS = (
    "Честно — не знаю.",
    "Тут я пас.",
    "Без понятия, врать не буду.",
    "Не в курсе этого вопроса.",
    "Такого в моей базе нет.",
    "Тут мне нечего добавить.",
    "Не знаю, увы.",
    "Тут я бессилен — данных нет.",
    "Не подскажу, нет информации.",
    "Молчу, потому что правда не знаю.",
    "Нет, этого я не знаю.",
    "Пас. Не хочу гадать.",
    "Тут у меня пробел.",
    "Не рискну ответить — нет данных.",
    "Не знаю. Бывает и такое.",
    "Такого в моих записях нет.",
    "Тут мне сказать нечего.",
    "Не отвечу — нет надёжной информации.",
    # Варианты с реальным полезным направлением — где это оправдано
    "Не знаю точно. По таким вопросам надёжнее в УК «ВЕК»: +7 (495) 401-60-06.",
    "Нет у меня данных. Если срочно — аварийная: +7 (495) 085-33-30.",
    "Не подскажу. Это профильный вопрос для УК: +7 (495) 401-60-06.",
)


# Последний использованный style-hint per (chat_id, user_id) — чтобы не повторялся подряд.
_LAST_STYLE_HINT_BY_USER: dict[tuple[int, int], str] = {}


_SMALLTALK_PATTERNS = re.compile(
    r"(?ix)"
    r"\b(привет|здаров|здорово|здравствуй|хай|hello|hi|добрый\s+(день|вечер|утро)|"
    r"как\s+дел[аои]|как\s+ты|как\s+жизнь|что\s+нового|"
    r"что\s+думаешь|как\s+вам|как\s+тебе|"
    r"расскажи|поговорим|поболтаем|"
    r"спасибо|благодар|пасиб|"
    r"опять|достал[оa]|задолбал|надоел|бесит|"
    r"ох+|эх+|ну\s+вот|ужас|кошмар|"
    r"шут[ия]|прикол|смешно|"
    r"скучно|грустно|устал)\b"
)


def _looks_like_smalltalk(text: str) -> bool:
    """Грубая эвристика: похоже ли сообщение на болтовню/эмоцию, а не на фактологический запрос."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) <= 25 and ("?" in stripped or "!" in stripped or any(ch.isalpha() for ch in stripped)):
        # Очень короткие сообщения чаще всего болтовня
        if not re.search(r"\b(где|когда|сколько|кто|как\s+попасть|телефон|адрес|номер|маршрут)\b", stripped, re.I):
            return True
    return bool(_SMALLTALK_PATTERNS.search(stripped))


_DAILY_SUMMARY_SYSTEM_PROMPT = (
    "Сформируй краткую сводку для админов чата ЖК на русском: до 800 символов, "
    "без таблиц, без персональных данных, нейтрально и по фактам."
)

_CONVERSATION_SUMMARY_PROMPT = (
    "Сожми переписку в 2-3 предложения на русском. Сохрани ключевые темы, "
    "вопросы и ответы. Не теряй факты, но убери повторы и несущественные детали. "
    "Результат — краткое резюме разговора, до 500 символов."
)

_FORBIDDEN_ASSISTANT_TOPICS = (
    "полит",
    "религи",
    "националь",
    "диагноз",
    "юрид",
    "адвокат",
    "финанс",
    "инвест",
    "кредит",
)
# Слова, которые НЕ блокируют ответ сами по себе (были раньше в запрещённых):
# "медицин" — пользователи спрашивают про мед. учреждения рядом
# "паспорт" — спрашивают про МФЦ и документы
# "телефон" — спрашивают номера телефонов инфраструктуры
# "email" — спрашивают контакты УК/сервисов
# "суд" — может быть в контексте бытовых жалоб
_RUDE_PATTERNS = (
    "убью",
    "убить",
    "сдохни",
    "уничтож",
    "калечить",
    "зарежу",
    "задушу",
    "прибью",
    "порву",
    "голову оторв",
    "башку оторв",
    "закопаю",
    "урою",
)
_FORBIDDEN_TOPIC_REPLIES = (
    "Не-не, это за пределами моих полномочий! Тут нужен профи, а я — по делам дома.",
    "Ого, тут я точно не эксперт. К специалисту бы! А по ЖК — спрашивай смело.",
    "Тут мои полномочия заканчиваются. Я про парковку, лифт и шлагбаум, а для этого — к специалисту 🏠",
    "Знаешь, я бы ответил, но совесть не позволяет — тут нужен профессионал. Зато по дому — всегда!",
    "Это выше моего уровня компетенции. Лучше к профи! А по ЖК — обращайся.",
    "Ух, тут я точно промолчу. Не хочу навредить советом. Но по дому — не подведу 😄",
    "Если бы я был экспертом в этом — не сидел бы в чате ЖК, правда? К профи!",
    "Честно: это не моя тема. Но если нужна помощь по дому — я тут как тут!",
    "Это как спрашивать у бота про космос — могу, но лучше не надо. К специалисту!",
    "Тут даже я краснею. Это к специалисту! А я — за уют и быт ЖК.",
)

_AGGRESSIVE_INSULT_PATTERNS = (
    "идиот",
    "дебил",
    "даун",
    "уродин",
    "мразь",
    "тварь",
    "ублюд",
    "дурак",
    "дура ",
    "тупой",
    "тупая",
    "тупица",
    "кретин",
    "придурок",
    "придурошн",
    "неадекват",
    "чмо",
    "лох",
    "лошар",
    "чушка",
    "свинья",
    "скотин",
    "отброс",
    "конченн",
    "конч ",
    "быдло",
    "шлюх",
    "шалав",
)

# Паттерны мягкой грубости — пассивная агрессия, снисходительность (severity 1)
_SOFT_AGGRESSION_PATTERNS = (
    "рот закрой",
    "заткнись",
    "завали",
    "чушь нес",
    "бред нес",
    "не лезь",
    "тебя не спраш",
    "тебя не просили",
    "вас не спраш",
    "вас не просили",
    "иди отсюда",
    "иди лесом",
    "иди нафиг",
    "пошёл вон",
    "пошла вон",
    "пошёл нах",
    "пошла нах",
    "отвали",
    "вали отсюда",
    "лечись",
    "лечиться надо",
    "к врачу сходи",
    "к психиатру",
    "к психологу сходи",
    "ты больной",
    "ты больная",
    "ты бешен",
)
_LATIN_TO_CYR = str.maketrans({
    "a": "а",
    "b": "в",
    "c": "с",
    "e": "е",
    "h": "н",
    "k": "к",
    "m": "м",
    "o": "о",
    "p": "р",
    "t": "т",
    "x": "х",
    "y": "у",
})
_DIGIT_TO_CYR = str.maketrans({"0": "о", "1": "и", "3": "з", "4": "ч", "6": "б"})

PHONE_RE = re.compile(r"(?:\+7|8)\d{10}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
FULLNAME_RE = re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?\b")


@dataclass(slots=True)
class ModerationDecision:
    violation_type: Literal["none", "profanity", "rude", "aggression"]
    severity: int
    confidence: float
    action: Literal["none", "warn", "delete_warn", "delete_strike"]
    used_fallback: bool
    sentiment: Literal["positive", "neutral", "negative"] = "neutral"


@dataclass(slots=True)
class AiProbeResult:
    ok: bool
    details: str
    latency_ms: int


@dataclass(slots=True)
class AiRuntimeStatus:
    last_error: str | None
    last_error_at: datetime | None
    profanity_exact_count: int = 0
    profanity_prefix_count: int = 0
    profanity_exceptions_count: int = 0


@dataclass(slots=True)
class AiDiagnosticsReport:
    provider_mode: Literal["remote", "stub"]
    ai_enabled: bool
    has_api_key: bool
    api_url: str
    requests_used_today: int
    tokens_used_today: int
    probe_ok: bool
    probe_details: str
    probe_latency_ms: int


@dataclass(slots=True)
class RagCategorizationResult:
    category: str
    summary: str
    used_fallback: bool


class AiProvider(Protocol):
    async def probe(self) -> AiProbeResult: ...

    async def moderate(self, text: str, *, chat_id: int, context: list[str] | None = None) -> ModerationDecision: ...

    async def assistant_reply(
        self, prompt: str, context: list[str], *, chat_id: int,
        user_id: int | None = None, topic_id: int | None = None,
    ) -> str: ...

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str | None: ...

    async def categorize_rag_entry(self, text: str, *, chat_id: int) -> RagCategorizationResult: ...

    async def summarize_conversation(self, conversation: str, *, chat_id: int) -> str: ...

    async def extract_user_facts(self, dialog: str, *, chat_id: int) -> str: ...


class StubAiProvider:
    """Почему: стабильно возвращает локальное поведение до реального подключения ИИ."""

    async def probe(self) -> AiProbeResult:
        return AiProbeResult(False, "ИИ отключен: используется stub-провайдер.", 0)

    async def moderate(self, text: str, *, chat_id: int, context: list[str] | None = None) -> ModerationDecision:
        decision = local_moderation(text)
        decision.used_fallback = True
        return decision

    async def assistant_reply(
        self, prompt: str, context: list[str], *, chat_id: int,
        user_id: int | None = None, topic_id: int | None = None,
    ) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return random.choice(_FORBIDDEN_TOPIC_REPLIES)
        places_context = await _get_places_context(safe_prompt)
        rag_text = await _get_rag_context(chat_id, safe_prompt)
        faq_answer = await _get_faq_answer(chat_id, safe_prompt)
        web_hint = ""
        if should_search_web(safe_prompt) and not rag_text and not faq_answer:
            try:
                web_results = await search_duckduckgo(safe_prompt)
                web_hint = format_search_context(web_results)
            except Exception:
                pass
        return build_local_assistant_reply(safe_prompt, context=context, places_hint=places_context, rag_hint=rag_text, faq_hint=faq_answer, web_hint=web_hint, user_id=user_id, topic_id=topic_id)

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str | None:
        return None

    async def categorize_rag_entry(self, text: str, *, chat_id: int) -> RagCategorizationResult:
        from app.services.rag import classify_rag_message
        category = classify_rag_message(text)
        summary = text[:200]
        return RagCategorizationResult(category=category, summary=summary, used_fallback=True)

    async def summarize_conversation(self, conversation: str, *, chat_id: int) -> str:
        # Простое обрезание без LLM
        lines = conversation.strip().split("\n")
        user_lines = [l for l in lines if l.startswith("user:")]
        return "Ранее обсуждали: " + "; ".join(l[6:].strip()[:80] for l in user_lines)[:500]

    async def extract_user_facts(self, dialog: str, *, chat_id: int) -> str:
        return "{}"


class OpenRouterProvider:
    """Почему: подключаем реальный ИИ через API без изменения публичных интерфейсов бота."""

    def __init__(self) -> None:
        base_url = settings.ai_api_url or "https://openrouter.ai/api/v1"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.ai_timeout_seconds),
        )
        self._model = _normalize_model_id(settings.ai_model)
        if self._model != settings.ai_model:
            logger.warning("AI model id normalized: %r -> %r", settings.ai_model, self._model)
        self._retries = max(0, settings.ai_retries)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _is_anthropic_model(self) -> bool:
        """OpenRouter использует префикс `anthropic/` для моделей Claude."""
        return (self._model or "").lower().startswith("anthropic/")

    def _build_system_message(self, static_text: str, dynamic_text: str) -> dict:
        """
        Собирает system-сообщение. Для Anthropic-моделей оборачивает статичную часть
        в content-block с `cache_control: ephemeral` — это включает Anthropic prompt
        caching через OpenRouter и экономит токены/латентность на повторных запросах.

        Для остальных провайдеров возвращает обычное сообщение со строковым content.
        """
        if self._is_anthropic_model():
            content_blocks: list[dict] = [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            if dynamic_text:
                content_blocks.append({"type": "text", "text": dynamic_text})
            return {"role": "system", "content": content_blocks}

        if dynamic_text:
            return {"role": "system", "content": f"{static_text}\n\n{dynamic_text}"}
        return {"role": "system", "content": static_text}

    async def _chat_completion(self, messages: list[dict], *, chat_id: int, temperature: float = 0.8, bypass_limit: bool = False) -> tuple[str, int]:
        if not settings.ai_key:
            raise RuntimeError("AI_KEY не задан")
        if not bypass_limit:
            allowed, reason = await _can_use_remote_ai(chat_id)
            if not allowed:
                raise RuntimeError(f"AI лимит: {reason or 'превышен'}")

        payload = {
            "temperature": temperature,
            "max_tokens": settings.ai_max_tokens,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {settings.ai_key}",
            "Content-Type": "application/json",
        }
        model_id = self._model
        used_fallback_model = False

        for attempt in range(self._retries + 1):
            payload["model"] = model_id
            logger.info("AI request -> model=%s chat_id=%s", model_id, chat_id)
            try:
                response = await self._client.post("/chat/completions", json=payload, headers=headers)
                if response.status_code >= 500 and attempt < self._retries:
                    continue
                response.raise_for_status()
                data = response.json()
                content = _strip_think_tags(_extract_response_content(data))
                if not content:
                    raise RuntimeError("AI вернул пустой текст (только think-теги)")
                tokens = int(data.get("usage", {}).get("total_tokens") or 0)
                await _add_remote_usage(chat_id, tokens)
                if used_fallback_model and self._model != model_id:
                    logger.warning("AI model switched to fallback for runtime stability: %r", model_id)
                    self._model = model_id
                logger.info("AI response <- tokens=%s chat_id=%s", tokens, chat_id)
                return content, tokens
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                response_text = exc.response.text[:500].strip()
                logger.warning(
                    "AI HTTP error status=%s chat_id=%s body=%r",
                    status_code,
                    chat_id,
                    response_text,
                )
                if status_code == 429 and attempt < self._retries:
                    continue
                error_hint = ""
                try:
                    error_payload = exc.response.json()
                    error_hint = str(error_payload.get("error", {}).get("message") or "")[:160]
                except ValueError:
                    error_hint = response_text[:160]
                if (
                    status_code in (400, 404, 422)
                    and _is_invalid_model_id_error(error_hint)
                    and not used_fallback_model
                    and model_id != _MODEL_FALLBACK_ID
                ):
                    logger.warning(
                        "AI invalid model id, retrying with fallback model: %r -> %r",
                        model_id,
                        _MODEL_FALLBACK_ID,
                    )
                    model_id = _MODEL_FALLBACK_ID
                    used_fallback_model = True
                    continue
                if error_hint:
                    raise RuntimeError(f"AI API вернул ошибку {status_code}: {error_hint}") from exc
                raise RuntimeError(f"AI API вернул ошибку {status_code}") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._retries:
                    raise RuntimeError("Сбой соединения с AI API") from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("Некорректный ответ AI API") from exc
        raise RuntimeError("AI API недоступен")

    async def probe(self) -> AiProbeResult:
        """Лёгкая проверка доступности API: GET /models без расхода токенов."""
        if not settings.ai_key:
            return AiProbeResult(False, "AI_KEY не задан", 0)
        started = time.perf_counter()
        try:
            response = await self._client.get(
                "/models",
                headers={"Authorization": f"Bearer {settings.ai_key}"},
            )
            latency = int((time.perf_counter() - started) * 1000)
            if response.status_code == 200:
                return AiProbeResult(True, "AI API доступен.", latency)
            return AiProbeResult(
                False,
                f"AI API вернул HTTP {response.status_code}",
                latency,
            )
        except Exception as exc:  # noqa: BLE001
            latency = int((time.perf_counter() - started) * 1000)
            return AiProbeResult(False, str(exc), latency)

    def _record_runtime_error(self, error: Exception) -> None:
        global _LAST_ERROR, _LAST_ERROR_AT
        _LAST_ERROR = str(error)
        _LAST_ERROR_AT = datetime.now(timezone.utc)
        logger.warning("AI provider error: %s", error)

    async def moderate(self, text: str, *, chat_id: int, context: list[str] | None = None) -> ModerationDecision:
        try:
            user_content = ""
            if context:
                user_content = "Контекст беседы (последние сообщения):\n"
                user_content += "\n".join(context[-8:]) + "\n\n"
            user_content += f"Сообщение для проверки:\n{text[:2000]}"

            content, _ = await self._chat_completion(
                [
                    {"role": "system", "content": _MODERATION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                chat_id=chat_id,
            )
            data = json.loads(content)
            violation_type = str(data.get("violation_type", "none"))
            action = str(data.get("action", "none"))
            severity = int(data.get("severity", 0))
            confidence = float(data.get("confidence", 0.5))
            sentiment = str(data.get("sentiment", "neutral"))
            if violation_type not in {"none", "profanity", "rude", "aggression"}:
                violation_type = "none"
            if action not in {"none", "warn", "delete_warn", "delete_strike"}:
                action = "none"
            if sentiment not in {"positive", "neutral", "negative"}:
                sentiment = "neutral"
            severity = max(0, min(3, severity))
            confidence = max(0.0, min(1.0, confidence))
            return ModerationDecision(violation_type, severity, confidence, action, False, sentiment)
        except (RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record_runtime_error(exc)
            decision = local_moderation(text)
            decision.used_fallback = True
            return decision

    async def assistant_reply(
        self, prompt: str, context: list[str], *, chat_id: int,
        user_id: int | None = None, topic_id: int | None = None,
    ) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return random.choice(_FORBIDDEN_TOPIC_REPLIES)

        # KB используется как контекст для AI, а не как прямой ответ.
        # Раньше при совпадении KB возвращался сырой текст из JSON без обработки AI,
        # что приводило к шаблонным ответам без учёта контекста вопроса.
        # Теперь AI всегда обрабатывает ответ, а KB предоставляет фактические данные.

        rag_text = await _get_rag_context(chat_id, safe_prompt)
        faq_answer = await _get_faq_answer(chat_id, safe_prompt)
        places_context = await _get_places_context(safe_prompt)

        # Статичная (кэшируемая) часть system-промпта.
        static_system_prompt = _ASSISTANT_SYSTEM_PROMPT
        # Динамическая часть: топик-хинт, KB, RAG, FAQ, places, web. Меняется
        # от запроса к запросу — НЕ кэшируется.
        dynamic_system_parts: list[str] = []

        topic_hint = get_topic_hint(topic_id)
        if topic_hint:
            dynamic_system_parts.append(topic_hint.lstrip("\n"))

        # Рандомный hint стиля для вариативности ответов
        style_hints = (
            "\n[Стиль: ответь одним предложением, без вступлений и финальных советов]",
            "\n[Стиль: прямой ответ, без вступлений — чётко и по делу]",
            "\n[Стиль: сухо и по-деловому, без шуток и эмодзи]",
            "\n[Стиль: нейтрально-информативно, как короткая справка]",
            "\n[Стиль: сдержанно, без эмодзи и восклицаний]",
            "\n[Стиль: коротко и тепло, в 1-2 фразы]",
            "\n[Стиль: задумчиво, с лёгким сомнением — если не уверен, так и скажи]",
            "\n[Стиль: начни с сочувствия (одной фразой), потом суть]",
            "\n[Стиль: начни с факта из контекста, без воды]",
            "\n[Стиль: будь кратким и деловым, как сосед, который спешит]",
            "\n[Стиль: используй разговорный тон, как в переписке с другом — но коротко]",
            "\n[Стиль: ответь непринуждённо, как бы между делом]",
            "\n[Стиль: ответь с самоиронией, без самолюбования]",
            "\n[Стиль: ответь как мудрый старожил, который всё видел — спокойно]",
            "\n[Стиль: ответь как опытный сосед, который через это прошёл]",
            "\n[Стиль: начни задумчиво, потом дай чёткий ответ]",
            "\n[Стиль: минималистично — только то, что нужно знать]",
            "\n[Стиль: спокойно и по сути, без эмоций]",
            "\n[Стиль: лёгкая шутка в тему — если тема позволяет, иначе без шутки]",
            "\n[Стиль: как короткая заметка в блокноте — факт и всё]",
        )
        # Выбираем style-hint, исключая последний использованный для этого юзера, чтобы не повторялся подряд.
        # Hint вынесен в финальное user-сообщение (не в system), чтобы:
        #   1) не ломать prompt caching статичного system-промпта;
        #   2) Claude точнее воспринимал one-shot инструкцию по стилю рядом с запросом.
        _last_hint_key = (chat_id or 0, user_id or 0)
        _prev_hint = _LAST_STYLE_HINT_BY_USER.get(_last_hint_key)
        _hint_pool = [h for h in style_hints if h != _prev_hint] or list(style_hints)
        chosen_hint = random.choice(_hint_pool).strip()
        _LAST_STYLE_HINT_BY_USER[_last_hint_key] = chosen_hint

        resident_context = build_resident_context(safe_prompt, context=context)

        # Веб-поиск: если вопрос выходит за рамки локальной базы знаний
        web_context = ""
        if should_search_web(safe_prompt) and not resident_context and not rag_text and not faq_answer:
            try:
                web_results = await search_duckduckgo(safe_prompt)
                web_context = format_search_context(web_results)
            except Exception:
                logger.warning("Веб-поиск при ответе ассистента не удался.")

        # Логируем какие контексты были найдены и источник ответа
        has_factual_context = bool(resident_context) or bool(rag_text) or bool(faq_answer) or bool(places_context)
        if resident_context:
            _answer_source = "resident_kb_context"
        elif rag_text:
            _answer_source = "rag"
        elif faq_answer:
            _answer_source = "faq"
        elif places_context:
            _answer_source = "places"
        elif web_context:
            _answer_source = "web"
        else:
            _answer_source = "fallback"
        logger.info(
            "ANSWER_SOURCE: source=%s resident_ctx=%s rag=%s faq=%s places=%s web=%s prompt=%r user_id=%s topic_id=%s",
            _answer_source, bool(resident_context), bool(rag_text), bool(faq_answer),
            bool(places_context), bool(web_context),
            safe_prompt[:80], user_id, topic_id,
        )

        if resident_context:
            dynamic_system_parts.append(
                "<knowledge_base source=\"resident_canonical\">\n"
                f"{resident_context}\n"
                "</knowledge_base>"
            )
        if rag_text:
            dynamic_system_parts.append(
                f"<knowledge_base source=\"rag\">\n{rag_text}\n</knowledge_base>"
            )
        if faq_answer:
            dynamic_system_parts.append(
                "<knowledge_base source=\"faq\">\n"
                f"{faq_answer}\n"
                "</knowledge_base>"
            )
        if places_context:
            dynamic_system_parts.append(
                "<knowledge_base source=\"places\">\n"
                f"{places_context}\n"
                "</knowledge_base>"
            )
        if web_context:
            dynamic_system_parts.append(
                f"<knowledge_base source=\"web\">\n{web_context}\n</knowledge_base>"
            )

        if not has_factual_context:
            dynamic_system_parts.append(
                "<no_kb_notice>В knowledge_base нет данных по этому вопросу. "
                "Не выдумывай факты о ЖК — честно скажи, что не знаешь, и "
                "предложи спросить в чате у соседей или в УК.</no_kb_notice>"
            )

        dynamic_system_text = "\n\n".join(dynamic_system_parts).strip()

        # Сборка system-сообщения. Для Anthropic-моделей через OpenRouter
        # используем content-blocks с cache_control на статичной части —
        # это даёт prompt caching и заметную экономию токенов/латентности.
        system_message = self._build_system_message(static_system_prompt, dynamic_system_text)

        # Формируем историю как отдельные user/assistant сообщения.
        # Гибридная обрезка: последние 6 реплик — до 1500 символов, остальные — до 500.
        messages: list[dict] = [system_message]
        history_window = context[-30:]
        recent_cutoff = max(0, len(history_window) - 6)
        for idx, line in enumerate(history_window):
            char_limit = 1500 if idx >= recent_cutoff else 500
            if line.startswith("user:"):
                messages.append({"role": "user", "content": line[5:].strip()[:char_limit]})
            elif line.startswith("assistant:"):
                messages.append({"role": "assistant", "content": line[10:].strip()[:char_limit]})

        # Style-hint добавляем к финальному запросу пользователя — рядом с
        # текстом, который Claude должен переформулировать.
        # Жёсткое напоминание прямо в user-сообщении: короче, без шаблонных концовок.
        _hard_reminder = (
            "[Напоминание: максимум 400 символов. Не заканчивай фразами "
            "«спроси в чате», «соседи подскажут», «кидай в чат» — человек уже здесь. "
            "Не начинай с «классика», «знакомо», «классический случай». "
            "Если не знаешь — скажи это одной фразой и всё.]"
        )
        if chosen_hint:
            final_user_text = f"{chosen_hint}\n{_hard_reminder}\n\n{safe_prompt}"
        else:
            final_user_text = f"{_hard_reminder}\n\n{safe_prompt}"
        messages.append({"role": "user", "content": final_user_text})

        # Динамическая температура: ниже для фактов, выше для болтовни.
        # Подкручено под Haiku 4.5 — модель и так живая, лишняя температура
        # ухудшает следование фактам и приводит к шаблонным «фантазиям».
        if has_factual_context:
            temperature = 0.5
        elif _looks_like_smalltalk(safe_prompt):
            temperature = 0.75
        else:
            temperature = 0.7
        try:
            content, _ = await self._chat_completion(messages, chat_id=chat_id, temperature=temperature)
            reply = content[:500]
            return reply
        except RuntimeError as exc:
            self._record_runtime_error(exc)
            return build_local_assistant_reply(safe_prompt, context=context, places_hint=places_context, rag_hint=rag_text, faq_hint=faq_answer)

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str | None:
        try:
            content, _ = await self._chat_completion(
                [
                    {"role": "system", "content": _DAILY_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": context[:4000]},
                ],
                chat_id=chat_id,
            )
            return content[:800]
        except RuntimeError as exc:
            self._record_runtime_error(exc)
            return None

    async def categorize_rag_entry(self, text: str, *, chat_id: int) -> RagCategorizationResult:
        try:
            content, _ = await self._chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "Категоризируй сообщение из чата ЖК. Верни только JSON:\n"
                            '{"category":"парковка|лифт|ук|коммуналка|безопасность|детская_площадка|'
                            'коммунальные_сервисы|безопасность_и_доступ|платежи|ремонт|правила|общее","summary":"краткая выжимка до 200 символов"}\n'
                            "Категория должна отражать основную тему сообщения.\n"
                            "Summary — это сжатая версия ключевых фактов без лишних деталей."
                        ),
                    },
                    {"role": "user", "content": text[:2000]},
                ],
                chat_id=chat_id,
            )
            data = json.loads(content)
            category = str(data.get("category", "общее"))
            summary = str(data.get("summary", text[:200]))[:200]
            valid_categories = {
                "парковка", "лифт", "ук", "коммуналка", "безопасность",
                "детская_площадка", "коммунальные_сервисы", "безопасность_и_доступ",
                "платежи", "ремонт", "правила", "общее",
            }
            if category not in valid_categories:
                category = "общее"
            return RagCategorizationResult(category=category, summary=summary, used_fallback=False)
        except (RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record_runtime_error(exc)
            from app.services.rag import classify_rag_message
            category = classify_rag_message(text)
            return RagCategorizationResult(category=category, summary=text[:200], used_fallback=True)

    async def summarize_conversation(self, conversation: str, *, chat_id: int) -> str:
        try:
            content, _ = await self._chat_completion(
                [
                    {"role": "system", "content": _CONVERSATION_SUMMARY_PROMPT},
                    {"role": "user", "content": conversation[:3000]},
                ],
                chat_id=chat_id,
            )
            return content[:500]
        except RuntimeError as exc:
            self._record_runtime_error(exc)
            # Fallback — простое обрезание
            lines = conversation.strip().split("\n")
            user_lines = [l for l in lines if l.startswith("user:")]
            return "Ранее обсуждали: " + "; ".join(l[6:].strip()[:80] for l in user_lines)[:500]

    async def extract_user_facts(self, dialog: str, *, chat_id: int) -> str:
        """Извлекает факты о пользователе из диалога через AI."""
        from app.services.resident_profile import EXTRACT_FACTS_PROMPT
        try:
            content, _ = await self._chat_completion(
                [
                    {"role": "system", "content": EXTRACT_FACTS_PROMPT},
                    {"role": "user", "content": dialog[:2000]},
                ],
                chat_id=chat_id,
            )
            return content[:500]
        except RuntimeError as exc:
            self._record_runtime_error(exc)
            return "{}"


async def _enrich_context(
    context: list[str],
    chat_id: int,
    user_id: int,
    topic_id: int | None,
) -> list[str]:
    """Обогащает контекст профилем жителя и настроением чата."""
    enriched = list(context)

    # Профиль жителя
    if settings.ai_feature_profiles:
        try:
            from app.services.resident_profile import format_profile_for_prompt, get_profile
            async for session in get_session():
                profile = await get_profile(session, user_id, chat_id)
                if profile:
                    profile_text = format_profile_for_prompt(profile)
                    if profile_text:
                        enriched.insert(0, f"summary: {profile_text}")
                break
        except Exception:
            logger.warning("Не удалось загрузить профиль жителя для контекста.")

    # Настроение чата
    if settings.ai_feature_mood:
        try:
            from app.services.mood import get_mood, get_mood_style_hint
            snapshot = get_mood(chat_id, topic_id)
            hint = get_mood_style_hint(snapshot.mood)
            if hint:
                enriched.insert(0, f"summary: {hint}")
        except Exception:
            logger.warning("Не удалось определить настроение чата.")

    return enriched


class AiModuleClient:
    """Почему: фасад для будущего ИИ, чтобы точки интеграции не трогать повторно."""

    def __init__(self, provider: AiProvider | None = None) -> None:
        self._provider = provider or StubAiProvider()

    async def aclose(self) -> None:
        close_method = getattr(self._provider, "aclose", None)
        if callable(close_method):
            await close_method()

    async def probe(self) -> AiProbeResult:
        return await self._provider.probe()

    async def moderate(self, text: str, *, chat_id: int, context: list[str] | None = None) -> ModerationDecision:
        try:
            return await asyncio.wait_for(
                self._provider.moderate(text, chat_id=chat_id, context=context),
                timeout=_MODERATION_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "AI moderation timeout after %s seconds; using local fallback.",
                _MODERATION_SOFT_TIMEOUT_SECONDS,
            )
            decision = local_moderation(text)
            decision.used_fallback = True
            return decision

    async def assistant_reply(
        self,
        prompt: str,
        context: list[str],
        *,
        chat_id: int,
        user_id: int | None = None,
        topic_id: int | None = None,
    ) -> str:
        # Инъекция профиля жителя и настроения чата в контекст
        enriched_context = list(context)
        if user_id is not None:
            enriched_context = await _enrich_context(enriched_context, chat_id, user_id, topic_id)
        try:
            return await asyncio.wait_for(
                self._provider.assistant_reply(
                    prompt, enriched_context, chat_id=chat_id,
                    user_id=user_id, topic_id=topic_id,
                ),
                timeout=_ASSISTANT_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "AI assistant timeout after %s seconds; using local fallback.",
                _ASSISTANT_SOFT_TIMEOUT_SECONDS,
            )
            places_context = await _get_places_context(prompt)
            rag_text = await _get_rag_context(chat_id, prompt)
            faq_answer = await _get_faq_answer(chat_id, prompt)
            return build_local_assistant_reply(prompt, context=context, places_hint=places_context, rag_hint=rag_text, faq_hint=faq_answer)

    async def assistant_reply_with_history(
        self,
        prompt: str,
        *,
        chat_id: int,
        user_id: int,
        context: list[str] | None = None,
    ) -> str:
        """Формирует summary из ChatHistory и добавляет его в контекст ответа."""
        history_summary = await build_dialog_summary_for_prompt(chat_id, user_id)
        base_context = context or []
        if history_summary:
            base_context = [history_summary, *base_context]
        return await self.assistant_reply(prompt, base_context, chat_id=chat_id, user_id=user_id)

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str | None:
        try:
            return await asyncio.wait_for(
                self._provider.generate_daily_summary(context, chat_id=chat_id),
                timeout=_SUMMARY_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "AI summary timeout after %s seconds; skipping summary.",
                _SUMMARY_SOFT_TIMEOUT_SECONDS,
            )
            return None

    async def categorize_rag_entry(self, text: str, *, chat_id: int) -> RagCategorizationResult:
        try:
            return await asyncio.wait_for(
                self._provider.categorize_rag_entry(text, chat_id=chat_id),
                timeout=_RAG_CATEGORIZE_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("AI RAG categorization timeout; using local fallback.")
            from app.services.rag import classify_rag_message
            category = classify_rag_message(text)
            return RagCategorizationResult(category=category, summary=text[:200], used_fallback=True)

    async def summarize_conversation(self, conversation: str, *, chat_id: int) -> str:
        try:
            return await asyncio.wait_for(
                self._provider.summarize_conversation(conversation, chat_id=chat_id),
                timeout=_SUMMARY_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("AI conversation summary timeout; using simple truncation.")
            lines = conversation.strip().split("\n")
            user_lines = [l for l in lines if l.startswith("user:")]
            return "Ранее обсуждали: " + "; ".join(l[6:].strip()[:80] for l in user_lines)[:500]

    async def extract_user_facts(self, dialog: str, *, chat_id: int) -> str:
        """Извлекает факты о пользователе из диалога (с таймаутом)."""
        try:
            return await asyncio.wait_for(
                self._provider.extract_user_facts(dialog, chat_id=chat_id),
                timeout=_SUMMARY_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("AI extract_user_facts timeout.")
            return "{}"


def _has_aggressive_target(text: str) -> bool:
    """Проверяет, направлена ли грубость на конкретного человека.

    Ищет комбинацию обращения (ты/вы) вместе с оскорбительным контекстом,
    а не просто наличие местоимений (они есть почти в каждом сообщении).
    Упоминания через @ намеренно не считаем прямым адресованием — пользователи
    часто обращаются к боту через @бот_username, и "@" не означает агрессию сам по себе.
    """
    lowered = text.lower()
    # Проверяем связки: местоимение + оскорбительное слово рядом
    direct_patterns = (
        "ты ", "тебя ", "тебе ", "тебой ",
        "вы ", "вас ", "вам ", "вами ",
    )
    has_pronoun = any(marker in lowered or lowered.startswith(marker.strip()) for marker in direct_patterns)
    if not has_pronoun:
        return False
    # Есть местоимение — проверяем наличие оскорбительных слов или агрессивных конструкций
    aggression_markers = (
        "идиот", "дебил", "даун", "тупой", "тупая", "дурак", "дура ",
        "мразь", "тварь", "ублюд", "кретин", "придурок", "неадекват",
        "чмо", "лох", "быдло", "скотин", "отброс",
        "заткнись", "завали", "отвали", "рот закрой",
        "больной", "больная", "бешен", "лечись",
    )
    return any(marker in lowered for marker in aggression_markers)


def local_moderation(text: str) -> ModerationDecision:
    normalized = normalize_for_profanity(text)
    lowered = text.lower()
    aggression_level = detect_aggression_level(text)

    # Угрозы физической расправой — всегда severity 3
    if any(pattern in lowered for pattern in _RUDE_PATTERNS):
        return ModerationDecision("aggression", 3, 0.9, "delete_strike", False)

    has_profanity = detect_profanity(normalized)
    has_insult = any(pattern in lowered for pattern in _AGGRESSIVE_INSULT_PATTERNS)
    has_soft_aggression = any(pattern in lowered for pattern in _SOFT_AGGRESSION_PATTERNS)
    has_target = _has_aggressive_target(text)

    # Прямое оскорбление конкретного человека с матом — severity 3
    if has_profanity and has_insult and has_target:
        return ModerationDecision("aggression", 3, 0.85, "delete_strike", False)

    # Оскорбление конкретного человека (без мата, но адресно) — severity 2
    if has_insult and has_target:
        return ModerationDecision("rude", 2, 0.8, "warn", False)

    # Мат с адресатом, но без прямого оскорбления — severity 2
    if has_profanity and has_target and aggression_level == "high":
        return ModerationDecision("profanity", 2, 0.75, "warn", False)

    # Мат с адресатом, низкая агрессия — severity 1
    if has_profanity and has_target:
        return ModerationDecision("profanity", 1, 0.7, "warn", False)

    # Пассивная агрессия / грубые команды адресно — severity 1
    if has_soft_aggression:
        return ModerationDecision("rude", 1, 0.7, "warn", False)

    # Мат без агрессии и адресата (бытовой мат) — severity 0, не наказываем
    if has_profanity:
        return ModerationDecision("none", 0, 0.6, "none", False)

    # Оскорбительные слова без адресата (жалоба на ситуацию) — severity 0
    if has_insult:
        return ModerationDecision("none", 0, 0.5, "none", False)

    return ModerationDecision("none", 0, 0.99, "none", False)


def normalize_for_profanity(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    lowered = lowered.translate(_LATIN_TO_CYR).translate(_DIGIT_TO_CYR)
    lowered = re.sub(r"[^а-яa-z0-9\s]+", "", lowered)
    return " ".join(lowered.split())


def detect_profanity(normalized: str) -> bool:
    if not normalized:
        return False

    for word in normalized.split():
        if word in _PROFANITY_RUNTIME["exceptions"]:
            continue
        if word in _PROFANITY_RUNTIME["exact"]:
            return True
        if any(word.startswith(prefix) for prefix in _PROFANITY_RUNTIME["prefixes"]):
            return True
    return False


def detect_aggression_level(text: str) -> Literal["low", "high"]:
    """Оценивает уровень агрессии для мягкой модерации."""
    lowered = text.lower()
    has_threat = any(pattern in lowered for pattern in _RUDE_PATTERNS)
    has_insult = any(pattern in lowered for pattern in _AGGRESSIVE_INSULT_PATTERNS)
    has_soft_aggression = any(pattern in lowered for pattern in _SOFT_AGGRESSION_PATTERNS)
    has_target = _has_aggressive_target(text)
    has_profanity = detect_profanity(normalize_for_profanity(text))

    if has_threat or (has_insult and has_target and has_profanity):
        return "high"
    if has_insult and has_target:
        return "high"
    if has_profanity and has_soft_aggression:
        return "high"
    return "low"


def mask_personal_data(text: str) -> str:
    text = PHONE_RE.sub("[скрыт_телефон]", text)
    text = EMAIL_RE.sub("[скрыт_email]", text)
    return FULLNAME_RE.sub("[скрыто_фио]", text)


def is_assistant_topic_allowed(text: str) -> bool:
    lowered = text.lower()
    if any(token in lowered for token in _FORBIDDEN_ASSISTANT_TOPICS):
        return False
    # Разрешаем любые запросы, которые не попадают в запрещённые темы.
    # Раньше фильтр отклонял всё без ключевых слов ЖК — это вызывало однотипные отказы.
    return True


def _normalize_assistant_prompt(prompt: str) -> str:
    """Убирает служебные префиксы из обращения, чтобы точнее определять интент."""
    cleaned = prompt.strip()
    cleaned = re.sub(r"^/ai(?:@\w+)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"@\w+", "", cleaned)
    return " ".join(cleaned.split())


_GATE_REPLIES = (
    "🚗 По шлагбауму:\n"
    "• Управление — через приложение «Дворецкий» (есть в App Store и Google Play)\n"
    "• Добавить своё авто — через форму УК или лично в офисе (2-й дом)\n"
    "• Гостевой пропуск — в приложении: Пропуска → + → день → номер авто → Выписать\n"
    "• Если номер гостя неизвестен — выпишите пустой пропуск, водитель вызовет диспетчера\n\n"
    "Что-то не работает? Опишите: какое авто, время, что именно произошло.",
    "🚗 Шлагбаум — краткая справка:\n"
    "• Приложение «Дворецкий» — скачайте для управления\n"
    "• Формат госномера: А123АА77 (заглавные, без пробелов)\n"
    "• Формат телефона: 79996668844 (без плюса)\n"
    "• Кнопка «Открыть» в приложении отключена — используйте вызывную панель\n\n"
    "Если нужна помощь с конкретной ситуацией — опишите подробнее.",
)

_NOISE_REPLIES = (
    "🔇 Проблема с шумом — порядок действий:\n"
    "1. Зафиксируйте: время, источник, длительность\n"
    "2. Поговорите с соседом (если известен)\n"
    "3. При повторах — обратитесь в УК: +7 (495) 401-60-06\n"
    "4. Ночной шум (23:00–07:00) — можно вызвать участкового: 8 (963) 922-58-51\n\n"
    "💡 В чате опишите ситуацию без эмоций и имён — факты решают быстрее.",
    "🔇 Шумят? Вот что делать:\n"
    "1. Запишите когда и откуда — точные данные важнее эмоций\n"
    "2. Напишите в профильную тему чата\n"
    "3. При повторах — заявка в УК «ВЕК»: +7 (495) 401-60-06\n"
    "4. Экстренные случаи — участковый: 8 (963) 922-58-51 или 102\n\n"
    "Опишите подробнее ситуацию — подскажу точнее.",
)

_COMPLAINT_REPLIES = (
    "📝 Как оформить жалобу, чтобы её решили быстрее:\n"
    "1. Где проблема: подъезд, этаж, двор, конкретное место\n"
    "2. Что случилось: опишите факты без эмоций\n"
    "3. Когда заметили: дата и время\n"
    "4. Фото/видео — сильно ускоряет обработку\n\n"
    "📞 Куда обращаться:\n"
    "• УК «ВЕК»: +7 (495) 401-60-06\n"
    "• Портал ЕДС: https://eds.mosreg.ru/\n"
    "• Срочные вопросы (аварии): +7 (495) 085-33-30",
    "📝 Жалоба? Вот формат для быстрого решения:\n"
    "• Где: подъезд / этаж / двор\n"
    "• Что: конкретная проблема\n"
    "• Когда: дата и время обнаружения\n"
    "• Фото — если есть, приложите обязательно\n\n"
    "Направляйте в УК «ВЕК»: +7 (495) 401-60-06 или через ЕДС (eds.mosreg.ru).",
)

_PARKING_REPLIES = (
    "🅿️ По парковке — как решить вопрос:\n"
    "1. Опишите ситуацию: где, когда, что мешает\n"
    "2. Без имён и госномеров других жильцов — это снижает конфликты\n"
    "3. Фото — желательно, но без персональных данных\n\n"
    "📞 Обращайтесь:\n"
    "• В УК «ВЕК»: +7 (495) 401-60-06\n"
    "• Нарушение ПДД — участковый: 8 (963) 922-58-51\n"
    "• Заблокировали выезд — аварийная: +7 (495) 085-33-30",
    "🅿️ Парковочный вопрос?\n"
    "• Факты: место, время, что мешает — без обвинений\n"
    "• Управление шлагбаумом: приложение «Дворецкий»\n"
    "• Проблемы с парковкой во дворе — в УК: +7 (495) 401-60-06\n\n"
    "Опишите подробнее — подскажу, к кому обратиться.",
)

_RULES_REPLIES = (
    "📜 Правила чата ЖК:\n"
    "✅ Можно: обсуждать вопросы ЖК, помогать советами, делиться полезной информацией\n"
    "❌ Нельзя: оскорбления, мат, угрозы, политика, религия, спам, чужие персональные данные\n\n"
    "💡 Главное — взаимоуважение. Мы все соседи!",
    "📜 Коротко о правилах:\n"
    "• Уважительный тон — без оскорблений и мата\n"
    "• Пишите в профильные темы (шлагбаум, жалобы, ремонт и т.д.)\n"
    "• Никакого спама и дублирования\n"
    "• Ссылки — только по теме ЖК\n"
    "• Персональные данные соседей — табу\n\n"
    "Подробности — в теме «Правила».",
)

_ELEVATOR_REPLIES = (
    "🛗 Проблема с лифтом:\n\n"
    "🔧 Не работает:\n"
    "• Лифтек: 8 (903) 779-11-63\n"
    "• УК «ВЕК»: +7 (495) 401-60-06\n"
    "Укажите: номер подъезда, этаж, что именно не так.\n\n"
    "🆘 Застряли:\n"
    "1. Кнопка вызова в кабине\n"
    "2. Лифтек: 8 (903) 779-11-63\n"
    "3. Экстренно: 112\n"
    "Не пытайтесь выбраться сами!",
    "🛗 По лифту:\n"
    "• Обслуживание — Лифтек: 8 (903) 779-11-63\n"
    "• УК: +7 (495) 401-60-06\n"
    "• Экстренная ситуация (застряли): кнопка вызова → Лифтек → 112\n\n"
    "Для заявки укажите: подъезд, время, что именно происходит.",
)

_TRASH_REPLIES = (
    "🗑️ Проблема с мусором:\n"
    "1. Сфотографируйте проблему (переполненные баки, грязь)\n"
    "2. Направьте обращение в УК: +7 (495) 401-60-06\n"
    "3. Или через ЕДС: https://eds.mosreg.ru/\n\n"
    "📦 Крупногабаритный мусор вывозят по отдельному графику — уточняйте в УК.\n"
    "УК обязана реагировать на обращения по чистоте территории.",
    "🗑️ По мусору:\n"
    "• Переполнены баки / грязно — фото + заявка в УК: +7 (495) 401-60-06\n"
    "• Крупный мусор — отдельный вывоз, график уточняйте в УК\n"
    "• Портал ЕДС: eds.mosreg.ru\n\n"
    "Укажите: что именно не так, где, когда обнаружили.",
)

_UTILITY_REPLIES = (
    "💰 По коммуналке:\n\n"
    "📊 Передача показаний — через приложение МособлЕИРЦ:\n"
    "• Электричество — с 15 по 26 число\n"
    "• Вода и отопление — с 10 по 19 число\n\n"
    "📞 Контакты:\n"
    "• УК «ВЕК»: +7 (495) 401-60-06\n"
    "• Мосэнергосбыт: +7 (499) 550-95-50\n\n"
    "⚠️ Не передали вовремя — начислят по среднему. Перерасчёт — заявление в УК.",
    "💰 Коммунальные вопросы:\n"
    "• Показания — через МособлЕИРЦ (электричество: 15–26, вода: 10–19 числа)\n"
    "• Расхождения в квитанции — запросите детализацию в УК: +7 (495) 401-60-06\n"
    "• Перерасчёт — письменное заявление в УК с основанием\n"
    "• Электричество — Мосэнергосбыт: +7 (499) 550-95-50\n\n"
    "Что именно интересует? Подскажу точнее.",
)

_NEIGHBOR_REPLIES = (
    "🏠 Конфликт с соседями — пошаговый план:\n"
    "1. Попробуйте поговорить лично и спокойно\n"
    "2. Не помогло — зафиксируйте проблему (даты, факты, фото)\n"
    "3. Обратитесь в УК «ВЕК»: +7 (495) 401-60-06\n"
    "4. Систематические нарушения — участковый: 8 (963) 922-58-51\n\n"
    "💡 В чате описывайте ситуацию без имён и эмоций — факты работают лучше.",
    "🏠 С соседями:\n"
    "• Первый шаг — личный разговор, спокойно и без обвинений\n"
    "• Не помогло — фиксируйте проблему и обращайтесь в УК: +7 (495) 401-60-06\n"
    "• Шум ночью / нарушения — участковый: 8 (963) 922-58-51\n"
    "• В экстренных ситуациях — 112\n\n"
    "Расскажите подробнее, что происходит — подскажу, к кому обратиться.",
)

_SECURITY_REPLIES = (
    "🔒 По безопасности:\n\n"
    "📞 Контакты:\n"
    "• УК «ВЕК»: +7 (495) 401-60-06\n"
    "• Участковый: 8 (963) 922-58-51\n"
    "• Экстренные случаи: 112\n\n"
    "📹 Видеонаблюдение: зарегистрируйтесь на Крепость24.рф (скан квитанции + ФИО). Техподдержка: 8-800-201-01-14.\n\n"
    "⚠️ Подозрительные лица / срочная угроза — сразу 112, потом в УК.",
    "🔒 Безопасность:\n"
    "• Домофон, камеры, охрана — обращайтесь в УК: +7 (495) 401-60-06\n"
    "• Видеонаблюдение — доступ через Крепость24.рф (техподдержка: 8-800-201-01-14)\n"
    "• Участковый: 8 (963) 922-58-51\n"
    "• Экстренно: 112\n\n"
    "Если что-то подозрительное — не тяните, звоните сразу.",
)


def _assistant_rule_reply(prompt: str) -> str | None:
    lowered = prompt.lower()
    rules_keywords = ("правил", "нельзя", "запрещ", "можно ли", "регламент")
    gate_keywords = ("шлагбаум", "пропуск", "въезд", "проезд", "пульт", "ворота")
    complaint_keywords = ("жалоб", "претенз", "не работает", "слом", "гряз", "протеч")
    noise_keywords = ("шум", "тих", "громк", "ноч", "ремонт")
    parking_keywords = ("парков", "машин", "авто", "место")
    elevator_keywords = ("лифт", "застрял", "кабин", "этаж не работ")
    trash_keywords = ("мусор", "контейнер", "бак", "свалк", "вывоз", "крупногабарит")
    utility_keywords = ("коммунал", "квитанц", "показани", "счётчик", "счетчик", "перерасч", "оплат")
    neighbor_keywords = ("сосед", "конфликт", "мешают", "шумят ночью", "курят")
    security_keywords = ("охран", "домофон", "камер", "видеонаблюд", "подозрит", "безопасн")

    if any(keyword in lowered for keyword in gate_keywords):
        return random.choice(_GATE_REPLIES)
    if any(keyword in lowered for keyword in elevator_keywords):
        return random.choice(_ELEVATOR_REPLIES)
    if any(keyword in lowered for keyword in noise_keywords):
        return random.choice(_NOISE_REPLIES)
    if any(keyword in lowered for keyword in complaint_keywords):
        return random.choice(_COMPLAINT_REPLIES)
    if any(keyword in lowered for keyword in parking_keywords):
        return random.choice(_PARKING_REPLIES)
    if any(keyword in lowered for keyword in trash_keywords):
        return random.choice(_TRASH_REPLIES)
    if any(keyword in lowered for keyword in utility_keywords):
        return random.choice(_UTILITY_REPLIES)
    if any(keyword in lowered for keyword in neighbor_keywords):
        return random.choice(_NEIGHBOR_REPLIES)
    if any(keyword in lowered for keyword in security_keywords):
        return random.choice(_SECURITY_REPLIES)
    if any(keyword in lowered for keyword in rules_keywords):
        return random.choice(_RULES_REPLIES)
    return None


def _pick_fallback_variant(seed_text: str) -> str:
    return random.choice(_FALLBACK_VARIANTS)


_EMPTY_PROMPT_REPLIES = (
    "Эй, ты позвал — я пришёл! Так что случилось?",
    "Слушаю! Кинь пару слов — разберёмся.",
    "О, вызвали, а вопрос забыли? Бывает! Пиши, что интересует.",
    "Тут, тут! Расскажи, что стряслось — помогу чем могу.",
    "Что у нас сегодня — лифт, парковка или что-то новенькое? 😄",
    "Привет! Я весь внимание. Давай подробности!",
    "Позвали — отвечаю! Что интересует? Пиши смело.",
    "На связи! Кидай вопрос — разберёмся.",
    "Слушаю-слушаю! Что там у тебя?",
    "Я тут! Формулируй вопрос — включаю суперслух.",
    "Стою, жду вопрос. Вот уже третью секунду жду. Не томи!",
    "На низком старте! Давай задачу — побежим решать.",
    "Упомянули — прибежал! Что нового в мире ЖК?",
    "Активирован! Уровень готовности: максимальный. Осталось только понять — к чему 😄",
)

_GREETING_REPLIES = (
    "О, привет! Как оно? Готов к подвигам 😄",
    "Здарова! Чё нового? Рассказывай!",
    "Привет-привет! На боевом посту, как всегда.",
    "О, сосед! Давно не виделись (ну, секунд 5). Чем помочь?",
    "Привет! Сегодня в хорошем настроении, спрашивай что угодно!",
    "Здравствуйте! К вашим услугам. Ну, в рамках разумного 😄",
    "Привет! Как всегда, караулю чат. Что случилось?",
    "Хэй! На месте. Лифт работает, шлагбаум тоже. Чем помочь?",
    "О, привет! Рад видеть живых людей. А то тут одни уведомления...",
    "Привет! Если ты с вопросом — отлично. Если просто поздороваться — тоже отлично!",
    "О, привет! Проходи, располагайся. Чай? Кофе? Или сразу к делу? 😄",
    "Здорово! Ну что, какие приключения сегодня? Лифт? Парковка? Шлагбаум?",
    "Привет! Уже третий час без вопросов. Спасён!",
    "Салют! Бодр и весел. Задавай вопрос, пока настрой боевой!",
    "Йо! В деле. Что нового в нашем уютном ЖК?",
)

_THANKS_REPLIES = (
    "Да не за что, обращайся! Тут для этого и стою 🙌",
    "Всегда пожалуйста! Помогать по делу — моё призвание.",
    "Рад, что помог! Если что — знаешь, где меня найти.",
    "Не за что! Помогать соседям — лучшая часть работы.",
    "Пожалуйста! Доволен, когда вопрос решён.",
    "Обращайся! Всегда на посту, даже ночью... ну, почти 😄",
    "Не за что! Хорошего дня, сосед!",
    "Без проблем! Если ещё что — пиши, не стесняйся.",
    "Это было легко! Обращайся ещё.",
    "Обращайся в любое время! Серьёзно, я не сплю.",
    "Пожалуйста! Ценю, когда говорят спасибо — это редкость!",
    "На здоровье! Заходи ещё — ответ всегда найдётся. Ну, или шутка.",
    "Спасибо, что спасибо! Растроган 😄",
    "Не за что, сосед! Будут вопросы — обращайся.",
)

_GREETING_PATTERNS = ("привет", "здравствуй", "добрый день", "добрый вечер", "доброе утро", "хай", "hello", "hi ", "хэй")
_THANKS_PATTERNS = ("спасибо", "благодар", "спс", "thanks", "мерси", "респект", "класс, спасиб")


def _detect_intent(text: str) -> str | None:
    """Определяет простой интент пользователя по ключевым словам."""
    lowered = text.lower().strip()
    if any(p in lowered for p in _GREETING_PATTERNS):
        return "greeting"
    if any(p in lowered for p in _THANKS_PATTERNS):
        return "thanks"
    return None


def build_local_assistant_reply(
    prompt: str,
    *,
    context: list[str] | None = None,
    places_hint: str | None = None,
    rag_hint: str | None = None,
    faq_hint: str | None = None,
    web_hint: str | None = None,
    user_id: int | None = None,
    topic_id: int | None = None,
) -> str:
    normalized_prompt = _normalize_assistant_prompt(prompt)
    if not normalized_prompt:
        return random.choice(_EMPTY_PROMPT_REPLIES)

    # Быстрые интенты: приветствие и благодарность
    intent = _detect_intent(normalized_prompt)
    if intent == "greeting":
        return random.choice(_GREETING_REPLIES)
    if intent == "thanks":
        return random.choice(_THANKS_REPLIES)

    # 1. Каноническая база знаний ЖК — используем только при очень точном совпадении
    # (exact match в паттернах), чтобы не отвечать шаблонно на вопросы с другим контекстом
    kb_result = search_resident_kb(normalized_prompt, context=context, top_k=1)
    if kb_result.exact and kb_result.matches and kb_result.matches[0].score >= 0.9:
        resident_answer = kb_result.matches[0].entry.answer
        logger.info("ANSWER_SOURCE: source=resident_kb_exact prompt=%r", normalized_prompt[:80])
        return resident_answer

    # 2. RAG — записи, добавленные админами через /rag_bot
    if rag_hint and rag_hint.strip():
        intros = (
            "О, тут я в теме!",
            "Так, у меня есть инфа по этому вопросу.",
            "Ага, знаю — вот что записано у нас:",
            "Секунду... Точно, вот:",
            "Этот вопрос мне знаком!",
            "У меня как раз было про это в записях.",
        )
        logger.info("ANSWER_SOURCE: source=rag prompt=%r", normalized_prompt[:80])
        return f"{random.choice(intros)}\n{rag_hint.strip()[:700]}"

    # 3. FAQ-ответ (закреплённый ответ, набравший положительные оценки)
    if faq_hint and faq_hint.strip():
        logger.info("ANSWER_SOURCE: source=faq prompt=%r", normalized_prompt[:80])
        return faq_hint.strip()[:800]

    # 4. Данные из БД инфраструктуры
    if places_hint and places_hint.strip():
        intros = (
            "Знаю такое место!",
            "Ага, есть инфа:",
            "По нашему району нашлось:",
            "Вот что у меня есть по этому адресу:",
            "Секунду... Точно, вот информация:",
        )
        logger.info("ANSWER_SOURCE: source=places prompt=%r", normalized_prompt[:80])
        return f"{random.choice(intros)}\n{places_hint.strip()[:700]}"

    # 6. Результаты веб-поиска
    if web_hint and web_hint.strip():
        intros = (
            "В моих записях не было, но погуглил для тебя:",
            "Покопался в интернете — вот что нашлось:",
            "Этого в нашей базе нет, но вот что удалось найти:",
        )
        logger.info("ANSWER_SOURCE: source=web prompt=%r", normalized_prompt[:80])
        return f"{random.choice(intros)}\n{web_hint.strip()[:700]}"

    # 7. Локальные правила-подсказки (шлагбаум, лифт, шум и т.д.)
    rule_reply = _assistant_rule_reply(normalized_prompt)
    if rule_reply:
        logger.info("ANSWER_SOURCE: source=rule prompt=%r", normalized_prompt[:80])
        return rule_reply

    # 8. Не знаю — честно говорю
    logger.info("ANSWER_SOURCE: source=fallback prompt=%r", normalized_prompt[:80])
    return _pick_fallback_variant(normalized_prompt)



async def _get_faq_answer(chat_id: int, query: str) -> str | None:
    question_key = _normalize_cache_key(query)
    if not question_key:
        return None
    try:
        async for session in get_session():
            return await get_faq_answer(session, chat_id=chat_id, question_key=question_key)
    except Exception as exc:
        logger.warning("FAQ search failed: %s", exc)
    return None



async def _get_rag_context(chat_id: int, query: str) -> str:
    """Подгружает весь RAG-контекст, ранжируя его по релевантности запроса."""
    try:
        from app.services.rag import build_rag_context, systematize_rag

        async for session in get_session():
            changed = await systematize_rag(session, chat_id)
            if changed:
                await session.commit()
            return await build_rag_context(session, chat_id=chat_id, query=query, top_k=8)
    except Exception as exc:
        logger.warning("RAG search failed: %s", exc)
    return ""


_PLACES_STOP_WORDS = {
    "где", "как", "что", "какой", "какая", "какие", "какое", "есть", "ли",
    "рядом", "ближайший", "ближайшая", "ближайшие", "около", "возле", "вблизи",
    "нужен", "нужна", "нужно", "нужны", "можно", "хочу", "подскажите",
    "посоветуйте", "скажите", "покажите", "найти", "найди", "ищу",
    "в", "на", "от", "до", "по", "из", "за", "к", "с", "у", "о",
    "и", "или", "а", "но", "не", "тут", "там", "мне", "нам",
    "этот", "эта", "это", "эти", "тот", "та", "те", "то",
    "очень", "самый", "самая", "самое", "самые", "всё", "все",
}

# Синонимы для расширения поисковых запросов по инфраструктуре
_PLACES_SYNONYMS: dict[str, list[str]] = {
    "мфц": ["мфц", "госуслуг"],
    "поликлиник": ["поликлиник", "врач", "медицин"],
    "больниц": ["больниц", "медицин", "стационар"],
    "аптек": ["аптек", "лекарств"],
    "стоматолог": ["стоматолог", "зубн"],
    "школ": ["школ", "образован"],
    "садик": ["садик", "детский сад"],
    "детсад": ["детский сад", "садик"],
    "почт": ["почт", "посылк"],
    "магазин": ["магазин", "продукт"],
    "продукт": ["продукт", "магазин", "супермаркет"],
    "кафе": ["кафе", "ресторан", "пекарн"],
    "ресторан": ["ресторан", "кафе"],
    "поесть": ["кафе", "ресторан", "пекарн"],
    "перекус": ["кафе", "пекарн"],
    "стройматериал": ["стройматериал", "ремонт"],
    "строймаг": ["стройматериал"],
    "леруа": ["леруа"],
    "торгов": ["торгов", "центр"],
    "пункт выдач": ["пункт выдач", "сдэк", "wildberries", "ozon"],
    "посылк": ["посылк", "почт", "сдэк"],
    "госучрежден": ["госучрежден", "мфц", "администрац"],
    "пенсион": ["пенсион", "сфр", "пособ"],
    "развива": ["развива", "детск", "кружк"],
}

_MIN_WORD_LENGTH = 3


def _word_search_variants(word: str) -> tuple[str, ...]:
    """Добавляет простой морфологический fallback для поиска по базе мест."""
    normalized = word.lower().strip()
    if len(normalized) < _MIN_WORD_LENGTH:
        return ()

    variants = [normalized]
    if len(normalized) >= 5 and normalized[-1] in {"а", "я", "ы", "и", "у", "ю", "е", "о", "ь"}:
        variants.append(normalized[:-1])
    # Расширяем синонимами
    for key, synonyms in _PLACES_SYNONYMS.items():
        if normalized.startswith(key) or key.startswith(normalized):
            variants.extend(synonyms)
    return tuple(dict.fromkeys(variants))


def _extract_search_words(query: str) -> list[str]:
    """Извлекает значимые слова из запроса для поиска по инфраструктуре."""
    words = re.findall(r"[а-яёa-z0-9]+", query.strip().lower())
    variants: list[str] = []
    for word in words:
        if len(word) < _MIN_WORD_LENGTH or word in _PLACES_STOP_WORDS:
            continue
        variants.extend(_word_search_variants(word))
    return list(dict.fromkeys(variants))


async def _get_places_context(query: str, *, top_k: int = 5) -> str:
    """Подбирает релевантные объекты инфраструктуры для AI-ответа."""
    search_words = _extract_search_words(query)
    logger.info("Places search: query=%r words=%s", query[:100], search_words[:5])
    if not search_words:
        return ""

    try:
        async for session in get_session():
            # Каждое слово должно встречаться хотя бы в одном из полей
            from sqlalchemy import and_, or_
            word_conditions = []
            for word in search_words[:5]:  # Ограничиваем количество слов
                like = f"%{word}%"
                word_conditions.append(
                    or_(
                        Place.name.ilike(like),
                        Place.address.ilike(like),
                        Place.category.ilike(like),
                        Place.subcategory.ilike(like),
                        Place.description.ilike(like),
                    )
                )
            rows = (
                await session.execute(
                    select(Place)
                    .where(Place.is_active.is_(True), or_(*word_conditions))
                    .order_by(Place.distance_km.asc().nulls_last(), Place.name.asc())
                    .limit(top_k)
                )
            ).scalars().all()
            if not rows:
                logger.info("Places search: no results found for words=%s", search_words[:5])
                return ""
            logger.info("Places search: found %d results", len(rows))

            parts: list[str] = []
            for item in rows:
                snippet = f"- {item.name} ({item.category}"
                if item.subcategory:
                    snippet += f" / {item.subcategory}"
                snippet += f"), адрес: {item.address}"
                if item.phone:
                    snippet += f", тел: {item.phone}"
                if item.website:
                    snippet += f", сайт: {item.website}"
                if item.work_time:
                    snippet += f", режим: {item.work_time}"
                if item.distance_km is not None:
                    snippet += f", расстояние: {item.distance_km:.1f} км"
                if item.description:
                    snippet += f" — {item.description[:100]}"
                parts.append(snippet)
            return "\n".join(parts)
    except Exception as exc:
        logger.warning("Places search failed: %s", exc)
    return ""



async def build_dialog_summary_for_prompt(chat_id: int, user_id: int, *, limit: int = 6) -> str:
    """Готовит короткое summary из последних реплик для системного промпта."""
    from app.models import ChatHistory

    try:
        async for session in get_session():
            rows = (
                await session.execute(
                    select(ChatHistory)
                    .where(ChatHistory.chat_id == chat_id, ChatHistory.user_id == user_id)
                    .order_by(ChatHistory.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            if not rows:
                return ""
            ordered = list(reversed(rows))
            parts = [f"{row.role}: {(row.message or row.text)[:120]}" for row in ordered]
            return "Краткий контекст диалога:\n" + "\n".join(parts)[:700]
    except Exception as exc:
        logger.warning("Failed to build dialogue summary: %s", exc)
    return ""




_AI_CLIENT: AiModuleClient | None = None
_AI_RUNTIME_ENABLED: bool = True
_ADMIN_ALERT_NOTIFIER: Callable[[str], Awaitable[None]] | None = None
_LAST_ERROR: str | None = None
_LAST_ERROR_AT: datetime | None = None
_PROFANITY_RUNTIME: dict[str, set[str]] = {"exact": set(), "prefixes": set(), "exceptions": set()}


def reload_profanity_runtime() -> dict[str, int]:
    """Перезагружает runtime-словарь мата и возвращает применённые размеры."""

    global _PROFANITY_RUNTIME
    _PROFANITY_RUNTIME = reload_profanity_runtime_dict()
    return {
        "exact": len(_PROFANITY_RUNTIME["exact"]),
        "prefixes": len(_PROFANITY_RUNTIME["prefixes"]),
        "exceptions": len(_PROFANITY_RUNTIME["exceptions"]),
    }


reload_profanity_runtime()


async def _can_use_remote_ai(chat_id: int) -> tuple[bool, str | None]:
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        allowed, reason = await can_consume_ai(
            session,
            date_key=date_key,
            chat_id=chat_id,
            request_limit=settings.ai_daily_request_limit,
            token_limit=settings.ai_daily_token_limit,
        )
        return allowed, reason
    return False, "не удалось получить сессию БД"


async def _add_remote_usage(chat_id: int, tokens: int) -> None:
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        await add_usage(session, date_key=date_key, chat_id=chat_id, tokens_used=tokens)
        return


def get_ai_runtime_status() -> AiRuntimeStatus:
    return AiRuntimeStatus(
        last_error=_LAST_ERROR,
        last_error_at=_LAST_ERROR_AT,
        profanity_exact_count=len(_PROFANITY_RUNTIME["exact"]),
        profanity_prefix_count=len(_PROFANITY_RUNTIME["prefixes"]),
        profanity_exceptions_count=len(_PROFANITY_RUNTIME["exceptions"]),
    )


def resolve_provider_mode() -> Literal["remote", "stub"]:
    """Возвращает фактический режим провайдера с учетом ключа и runtime-флага."""
    if settings.ai_enabled and bool(settings.ai_key) and is_ai_runtime_enabled():
        return "remote"
    return "stub"


async def get_ai_usage_for_today(chat_id: int) -> tuple[int, int]:
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        usage = await get_usage_stats(session, date_key=date_key, chat_id=chat_id)
        return usage.requests_used, usage.tokens_used
    return 0, 0


async def get_ai_diagnostics(chat_id: int) -> AiDiagnosticsReport:
    provider_mode = resolve_provider_mode()
    req_used, tok_used = await get_ai_usage_for_today(chat_id)
    probe_result = await get_ai_client().probe()
    return AiDiagnosticsReport(
        provider_mode=provider_mode,
        ai_enabled=settings.ai_enabled,
        has_api_key=bool(settings.ai_key),
        api_url=settings.ai_api_url or "https://openrouter.ai/api/v1",
        requests_used_today=req_used,
        tokens_used_today=tok_used,
        probe_ok=probe_result.ok,
        probe_details=probe_result.details,
        probe_latency_ms=probe_result.latency_ms,
    )


def set_ai_admin_notifier(notifier: Callable[[str], Awaitable[None]] | None) -> None:
    global _ADMIN_ALERT_NOTIFIER
    _ADMIN_ALERT_NOTIFIER = notifier


def is_ai_runtime_enabled() -> bool:
    return _AI_RUNTIME_ENABLED


def set_ai_runtime_enabled(value: bool) -> None:
    global _AI_RUNTIME_ENABLED, _AI_CLIENT, _LAST_ERROR, _LAST_ERROR_AT
    _AI_RUNTIME_ENABLED = value
    _AI_CLIENT = None
    if value:
        logger.info("AI runtime flag enabled.")
    else:
        _LAST_ERROR = "runtime_disabled"
        _LAST_ERROR_AT = datetime.now(timezone.utc)
        logger.info("AI runtime flag disabled; forcing stub mode.")


def get_ai_client() -> AiModuleClient:
    global _LAST_ERROR, _LAST_ERROR_AT
    global _AI_CLIENT
    if _AI_CLIENT is None:
        if settings.ai_enabled and settings.ai_key and is_ai_runtime_enabled():
            _AI_CLIENT = AiModuleClient(OpenRouterProvider())
            _LAST_ERROR = None
            _LAST_ERROR_AT = None
        else:
            _AI_CLIENT = AiModuleClient()
            if not is_ai_runtime_enabled():
                _LAST_ERROR = "runtime_disabled"
            else:
                _LAST_ERROR = "stub_mode"
            _LAST_ERROR_AT = datetime.now(timezone.utc)
    return _AI_CLIENT


async def close_ai_client() -> None:
    global _AI_CLIENT
    if _AI_CLIENT is None:
        return
    await _AI_CLIENT.aclose()
    _AI_CLIENT = None
