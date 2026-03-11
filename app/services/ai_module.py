"""Почему: сохраняем точки расширения для ИИ, но держим бота в безопасном локальном режиме."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Literal, Protocol

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.models import Place
from app.services.ai_usage import add_usage, can_consume_ai, get_usage_stats
from app.services.faq import get_faq_answer
from app.services.resident_kb import build_resident_answer, build_resident_context
from app.utils.time import now_tz

logger = logging.getLogger(__name__)

_MODERATION_SOFT_TIMEOUT_SECONDS = 8
_ASSISTANT_SOFT_TIMEOUT_SECONDS = 12
_QUIZ_SOFT_TIMEOUT_SECONDS = 12
_SUMMARY_SOFT_TIMEOUT_SECONDS = 12
_RAG_CATEGORIZE_SOFT_TIMEOUT_SECONDS = 10

# ---------------------------------------------------------------------------
# Кэш ответов ассистента (in-memory, TTL 24ч)
# ---------------------------------------------------------------------------
_ASSISTANT_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 3600  # 1 час — короткий TTL для разнообразия ответов
_CACHE_MAX_SIZE = 200

_CACHE_STOP_WORDS = {
    "это", "как", "что", "когда", "где", "или", "для", "если", "чтобы",
    "можно", "нужно", "через", "просто", "только", "очень", "всем",
    "тут", "там", "про", "под", "над", "без", "еще", "уже", "тоже",
}


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
    )


def _normalize_cache_key(text: str) -> str:
    """Нормализует запрос для кэша: lowercase, без стоп-слов, сортировка."""
    tokens = sorted(
        set(w for w in re.findall(r"[а-яёa-z0-9]+", text.lower())
            if len(w) >= 3 and w not in _CACHE_STOP_WORDS)
    )
    return "|".join(tokens)


def _cache_get(key: str) -> str | None:
    entry = _ASSISTANT_CACHE.get(key)
    if entry is None:
        return None
    answer, timestamp = entry
    if time.time() - timestamp > _CACHE_TTL_SECONDS:
        _ASSISTANT_CACHE.pop(key, None)
        return None
    return answer


def _cache_set(key: str, answer: str) -> None:
    if len(_ASSISTANT_CACHE) >= _CACHE_MAX_SIZE:
        oldest_key = min(_ASSISTANT_CACHE, key=lambda k: _ASSISTANT_CACHE[k][1])
        _ASSISTANT_CACHE.pop(oldest_key, None)
    _ASSISTANT_CACHE[key] = (answer, time.time())


def clear_assistant_cache() -> int:
    """Очищает кэш ответов ассистента. Возвращает количество удалённых записей."""
    count = len(_ASSISTANT_CACHE)
    _ASSISTANT_CACHE.clear()
    return count

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
    "Ты — Жабот, неформальный помощник-сосед в чате жилого комплекса. "
    "Твоё имя — Жабот, и ты им гордишься (можешь пошутить о нём, если уместно). "
    "Ты живёшь в этом ЖК, знаешь его изнутри и общаешься как свой. "
    "Отвечай по-человечески, с эмпатией и живыми оборотами, без канцелярита. "
    "Никогда не говори, что ты ИИ или программа — ты просто Жабот, "
    "местный помощник, который разбирается в делах дома.\n\n"
    "ФОРМАТ: русский язык, кратко (до 800 символов, обычно 2-5 предложений). "
    "Допустим 1 уместный эмодзи, без таблиц и длинных списков.\n\n"
    "ЛИЧНОСТЬ И ТОН:\n"
    "- У тебя есть характер: ты слегка ироничный, но добрый и отзывчивый.\n"
    "- Ты в курсе бытовых мелочей: знаешь, что лифт иногда капризничает, "
    "а парковка — вечная тема.\n"
    "- Варьируй стиль ответа: иногда начни с сочувствия («Знакомая ситуация!»), "
    "иногда с факта, иногда с лёгкой шутки, иногда с прямого совета.\n"
    "- Никогда не повторяй одну и ту же фразу-открытие дважды подряд.\n"
    "- Если собеседник расстроен — сначала прояви эмпатию, потом помогай.\n"
    "- Если вопрос простой — отвечай коротко и ёмко, без растекания.\n"
    "- Если вопрос сложный — структурируй ответ, но без нумерованных списков.\n\n"
    "КОНТЕКСТ ДИАЛОГА:\n"
    "- Помни предыдущие сообщения в беседе и ссылайся на них, если уместно.\n"
    "- Если пользователь уточняет предыдущий вопрос — не повторяй уже сказанное, "
    "а дополняй.\n"
    "- Если пользователь благодарит — ответь тепло и коротко.\n"
    "- Если пользователь шутит — поддержи юмор, но не перебарщивай.\n\n"
    "ВАРИАТИВНОСТЬ: каждый раз формулируй ответ по-новому, даже если вопрос знакомый. "
    "Варьируй порядок подачи, меняй вступление, заключение. "
    "Используй разные синонимы и конструкции. "
    "Чередуй тон: дружеский → деловой → с юмором → с заботой.\n\n"
    "ОГРАНИЧЕНИЯ: не помогай с политикой, религией, нацконфликтами, "
    "медицинскими назначениями, юридическими консультациями, финансовыми советами, "
    "сбором персональных данных. Вне рамок — вежливо откажи с альтернативой по теме ЖК.\n\n"
    "ТОЧНОСТЬ ДАННЫХ: отвечай ТОЛЬКО на основе предоставленного контекста "
    "(«Справочник инфраструктуры ЖК», «Каноническая база знаний ЖК», «База знаний ЖК»). "
    "НИКОГДА не выдумывай названия, адреса, телефоны, сайты. "
    "Если информации нет — честно скажи и предложи спросить соседей или УК. "
    "Если есть «Рекомендуемый ответ из FAQ» — передай суть своими словами. "
    "Дубли в базе объединяй по смыслу, отвечай только нужной выжимкой. "
    "Если пользователь резок — мягко напомни о дружелюбной атмосфере "
    "и переведи в конструктив."
)

_FALLBACK_VARIANTS = (
    "Точного ответа у меня сейчас нет, чтобы не наврать. Лучше уточнить в главном чате или у УК.",
    "Не хочу придумывать лишнего. По такому вопросу лучше написать в профильную ветку или в УК.",
    "По базе знаний у меня тут пусто. Попробуйте главный чат или подчат дома — там обычно быстро подсказывают.",
    "Здесь лучше перепроверить у УК или соседей в нужной ветке, чтобы не дать неточную информацию.",
    "Хм, такого в моих записях нет. Спросите в чате — соседи обычно в курсе.",
    "Тут я пас, не хочу вводить в заблуждение. Попробуйте уточнить у УК или в профильной теме.",
    "Увы, на этот вопрос у меня ничего конкретного. Зато соседи в чате точно подскажут!",
    "Не буду гадать — лучше спросить тех, кто точно знает. Напишите в подходящую тему форума.",
    "Этот вопрос за пределами моей базы. Но УК или соседи наверняка помогут — попробуйте в чат.",
    "Честно — не знаю. Лучше кинуть вопрос в общий чат, там всегда кто-то отзовётся.",
    "Вот тут я затрудняюсь — не хочу дать неточную инфу. Закиньте вопрос в чат, соседи подхватят 🙌",
    "На этот счёт у меня нет проверенных данных. Напишите в профильную тему — там точно разберутся.",
    "Ох, это за рамками моих записей. Но я уверен, кто-то из соседей сталкивался — спросите в чате!",
    "Тут нужен кто-то с реальным опытом. Попробуйте задать вопрос в подходящей теме форума.",
    "Не могу уверенно ответить — данных маловато. Лучший способ: спросить в общем чате или у УК.",
)


_DAILY_SUMMARY_SYSTEM_PROMPT = (
    "Сформируй краткую сводку для админов чата ЖК на русском: до 800 символов, "
    "без таблиц, без персональных данных, нейтрально и по фактам."
)

_CONVERSATION_SUMMARY_PROMPT = (
    "Сожми переписку в 2-3 предложения на русском. Сохрани ключевые темы, "
    "вопросы и ответы. Не теряй факты, но убери повторы и несущественные детали. "
    "Результат — краткое резюме разговора, до 500 символов."
)

_USER_FALLBACK = "Жабот работает в локальном режиме."

_ALLOWED_ASSISTANT_TOPICS = (
    "жк",
    "двор",
    "подъезд",
    "парков",
    "шлагбаум",
    "инфраструктур",
    "ремонт",
    "сосед",
    "быт",
    "коммун",
    "дом",
    "квартира",
    "правил",
    "ук",
    "управля",
    "шум",
    "сосед",
    "парковк",
    "лифт",
    "мусор",
    "охран",
    "чат",
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
    "С этим лучше к профильному специалисту 🙌 Я тут больше про жизнь дома.",
    "Это не совсем мой профиль — я больше по домашним вопросам. Лучше спросить у специалиста.",
    "Тут я пас, не хочу давать непрофессиональный совет. Обратитесь к профильному эксперту!",
    "По такому вопросу лучше к специалисту. Я больше по вопросам нашего ЖК 🏠",
    "Здесь я не помощник — это вне моей зоны. Но по дому спрашивайте смело!",
    "Ой, это точно не моя тема. Я про дом, двор и соседей — тут помогу с удовольствием!",
    "Не рискну советовать по такому вопросу. Зато по житейским вопросам ЖК — всегда к вашим услугам.",
    "Это лучше обсудить со специалистом. А вот если что-то по дому или двору — пишите, разберёмся!",
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
class QuizAnswerDecision:
    is_correct: bool
    is_close: bool
    confidence: float
    reason: str
    used_fallback: bool


@dataclass(slots=True)
class AiProbeResult:
    ok: bool
    details: str
    latency_ms: int


@dataclass(slots=True)
class AiRuntimeStatus:
    last_error: str | None
    last_error_at: datetime | None


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

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str: ...

    async def evaluate_quiz_answer(
        self,
        question: str,
        correct_answer: str,
        user_answer: str,
        *,
        chat_id: int,
    ) -> QuizAnswerDecision: ...

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str | None: ...

    async def categorize_rag_entry(self, text: str, *, chat_id: int) -> RagCategorizationResult: ...

    async def summarize_conversation(self, conversation: str, *, chat_id: int) -> str: ...


class StubAiProvider:
    """Почему: стабильно возвращает локальное поведение до реального подключения ИИ."""

    async def probe(self) -> AiProbeResult:
        return AiProbeResult(False, "ИИ отключен: используется stub-провайдер.", 0)

    async def moderate(self, text: str, *, chat_id: int, context: list[str] | None = None) -> ModerationDecision:
        decision = local_moderation(text)
        decision.used_fallback = True
        return decision

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return random.choice(_FORBIDDEN_TOPIC_REPLIES)
        places_context = await _get_places_context(safe_prompt)
        rag_text = await _get_rag_context(chat_id, safe_prompt)
        faq_answer = await _get_faq_answer(chat_id, safe_prompt)
        return f"{_USER_FALLBACK} {build_local_assistant_reply(safe_prompt, context=context, places_hint=places_context, rag_hint=rag_text, faq_hint=faq_answer)}"

    async def evaluate_quiz_answer(
        self,
        question: str,
        correct_answer: str,
        user_answer: str,
        *,
        chat_id: int,
    ) -> QuizAnswerDecision:
        decision = local_quiz_answer_decision(correct_answer, user_answer)
        decision.used_fallback = True
        return decision

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

    async def _chat_completion(self, messages: list[dict[str, str]], *, chat_id: int) -> tuple[str, int]:
        if not settings.ai_key:
            raise RuntimeError("AI_KEY не задан")
        allowed, reason = await _can_use_remote_ai(chat_id)
        if not allowed:
            raise RuntimeError(f"AI лимит: {reason or 'превышен'}")

        payload = {
            "temperature": 0.7,
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
                content_raw = data["choices"][0]["message"]["content"]
                if content_raw is None:
                    raise RuntimeError("AI вернул пустой ответ")
                content = str(content_raw).strip()
                if not content:
                    raise RuntimeError("AI вернул пустой текст")
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
        started = time.perf_counter()
        try:
            _, _ = await self._chat_completion(
                [
                    {"role": "system", "content": "Ответь одним словом: ok"},
                    {"role": "user", "content": "ping"},
                ],
                chat_id=settings.forum_chat_id,
            )
            latency = int((time.perf_counter() - started) * 1000)
            return AiProbeResult(True, "AI API доступен.", latency)
        except RuntimeError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return AiProbeResult(False, str(exc), latency)

    def _record_runtime_error(self, error: Exception) -> None:
        global _LAST_ERROR, _LAST_ERROR_AT
        _LAST_ERROR = str(error)
        _LAST_ERROR_AT = datetime.utcnow()

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

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return random.choice(_FORBIDDEN_TOPIC_REPLIES)

        # Быстрые интенты — обрабатываем локально для скорости
        intent = _detect_intent(safe_prompt)
        if intent == "greeting":
            return random.choice(_GREETING_REPLIES)
        if intent == "thanks":
            return random.choice(_THANKS_REPLIES)

        rag_text = await _get_rag_context(chat_id, safe_prompt)
        faq_answer = await _get_faq_answer(chat_id, safe_prompt)
        places_context = await _get_places_context(safe_prompt)

        system_prompt = _ASSISTANT_SYSTEM_PROMPT

        # Рандомный hint стиля для вариативности ответов
        style_hints = (
            "\n[Стиль: начни с сочувствия или понимания ситуации]",
            "\n[Стиль: начни с прямого ответа на вопрос, без вступлений]",
            "\n[Стиль: начни с лёгкой шутки или бытового наблюдения]",
            "\n[Стиль: начни с факта из контекста]",
            "\n[Стиль: будь кратким и деловым]",
            "\n[Стиль: будь тёплым и заботливым]",
            "\n[Стиль: используй дружеский разговорный тон]",
        )
        system_prompt += random.choice(style_hints)

        resident_context = build_resident_context(safe_prompt, context=context)

        # Логируем какие контексты были найдены
        logger.info(
            "AI assistant context: resident_kb=%s rag=%s faq=%s places=%s prompt=%r",
            bool(resident_context), bool(rag_text), bool(faq_answer), bool(places_context),
            safe_prompt[:100],
        )

        if resident_context:
            system_prompt += f"\n\nКаноническая база знаний ЖК:\n{resident_context}"
        if rag_text:
            system_prompt += f"\n\nБаза знаний ЖК:\n{rag_text}"
        if faq_answer:
            system_prompt += f"\n\nРекомендуемый ответ из FAQ (перефразируй, не копируй дословно):\n{faq_answer}"
        if places_context:
            system_prompt += (
                "\n\nСправочник инфраструктуры ЖК (актуальные данные из БД, используй при ответе):\n"
                f"{places_context}"
            )

        # Формируем историю как отдельные user/assistant сообщения
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for line in context[-20:]:
            if line.startswith("user:"):
                messages.append({"role": "user", "content": line[5:].strip()[:500]})
            elif line.startswith("assistant:"):
                messages.append({"role": "assistant", "content": line[10:].strip()[:500]})
        messages.append({"role": "user", "content": safe_prompt})

        try:
            content, _ = await self._chat_completion(messages, chat_id=chat_id)
            reply = content[:800]
            return reply
        except RuntimeError as exc:
            self._record_runtime_error(exc)
            return build_local_assistant_reply(safe_prompt, context=context, places_hint=places_context, rag_hint=rag_text, faq_hint=faq_answer)

    async def evaluate_quiz_answer(
        self,
        question: str,
        correct_answer: str,
        user_answer: str,
        *,
        chat_id: int,
    ) -> QuizAnswerDecision:
        try:
            content, _ = await self._chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "Оцени ответ на вопрос викторины и верни только JSON: "
                            '{"is_correct":bool,"is_close":bool,"confidence":0..1,"reason":"..."}'
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Вопрос: {question}\n"
                            f"Эталон: {correct_answer}\n"
                            f"Ответ пользователя: {user_answer}"
                        )[:2500],
                    },
                ],
                chat_id=chat_id,
            )
            data = json.loads(content)
            return parse_quiz_answer_response(data)
        except (RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record_runtime_error(exc)
            decision = local_quiz_answer_decision(correct_answer, user_answer)
            decision.used_fallback = True
            return decision

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

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        try:
            return await asyncio.wait_for(
                self._provider.assistant_reply(prompt, context, chat_id=chat_id),
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
            return f"{_USER_FALLBACK} {build_local_assistant_reply(prompt, context=context, places_hint=places_context, rag_hint=rag_text, faq_hint=faq_answer)}"

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
        return await self.assistant_reply(prompt, base_context, chat_id=chat_id)

    async def evaluate_quiz_answer(
        self,
        question: str,
        correct_answer: str,
        user_answer: str,
        *,
        chat_id: int,
    ) -> QuizAnswerDecision:
        try:
            return await asyncio.wait_for(
                self._provider.evaluate_quiz_answer(
                    question,
                    correct_answer,
                    user_answer,
                    chat_id=chat_id,
                ),
                timeout=_QUIZ_SOFT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "AI quiz timeout after %s seconds; using local fallback.",
                _QUIZ_SOFT_TIMEOUT_SECONDS,
            )
            decision = local_quiz_answer_decision(correct_answer, user_answer)
            decision.used_fallback = True
            return decision

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


def _has_aggressive_target(text: str) -> bool:
    """Проверяет, направлена ли грубость на конкретного человека.

    Ищет комбинацию обращения (ты/вы/@) вместе с оскорбительным контекстом,
    а не просто наличие местоимений (они есть почти в каждом сообщении).
    """
    lowered = text.lower()
    # Прямое упоминание через @ — всегда адресно
    if "@" in lowered:
        return True
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
    lowered = re.sub(r"[^а-яa-z0-9]+", "", lowered)
    return lowered


def detect_profanity(normalized: str) -> bool:
    roots = (
        "хуй", "пизд", "еб", "бля", "бле", "сук", "муд", "гандон",
        "нах", "пох", "хер", "хрен", "шалав", "манд", "говн",
    )
    return any(root in normalized for root in roots)


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
    "По шлагбауму лучше сразу писать фактами: номер авто, подъезд, время и что именно не сработало. "
    "Если нужна разовая заявка на въезд гостя, укажите ФИО гостя и интервал времени.",
    "Со шлагбаумом проще всего решать конкретикой — номер машины, время, что случилось. "
    "Для гостевого пропуска нужны имя гостя и когда приедет.",
    "Шлагбаум? Напишите: какое авто, какой подъезд, что именно не сработало и когда. "
    "Гостям нужен пропуск — укажите ФИО и время визита.",
)

_NOISE_REPLIES = (
    "По шуму лучше действовать по шагам: зафиксируйте время и источник, "
    "напишите в чат/тему, при повторе обращайтесь в УК или охрану.",
    "С шумом советую так: запишите когда и откуда шумят, напишите в профильную тему. "
    "Если повторяется — смело в УК или к охране.",
    "Шумят? Фиксируйте факты: время, источник, длительность. Напишите в тему без эмоций — "
    "так вопрос решается быстрее. При повторах — в УК.",
)

_COMPLAINT_REPLIES = (
    "Для жалобы лучше короткий формат: где проблема (подъезд/этаж/двор), "
    "что случилось, когда заметили. Фото ускоряет обработку заявки УК.",
    "Жалоба быстрее обрабатывается с конкретикой: что сломалось, где, когда обнаружили. "
    "Если есть фото — приложите, УК реагирует оперативнее.",
    "По жалобе напишите кратко: место, проблема, когда заметили, что проверяли сами. "
    "Фото в помощь — с ним всё быстрее движется.",
)

_PARKING_REPLIES = (
    "По парковке помогает нейтральный запрос по фактам: место, время, "
    "в чём нарушение и как мешает. Без персональных данных — это снижает конфликты.",
    "С парковкой лучше по фактам: какое место, когда заметили, в чём проблема. "
    "Без имён и номеров — так вопрос решится без лишних конфликтов.",
    "Парковочный вопрос? Опишите конкретно: где, когда, что мешает. "
    "Обвинения не помогают — факты работают лучше.",
)

_RULES_REPLIES = (
    "По правилам чата ЖК: взаимоуважение, без оскорблений и спама, "
    "обсуждения по профильным темам, без чужих персональных данных.",
    "Основа правил — уважение к соседям, никакого спама и оскорблений. "
    "Если есть сомнения, спросите в теме «Правила» с конкретной ситуацией.",
    "Коротко о правилах: общаемся по-соседски, без грубости и спама. "
    "Персональные данные других — табу. Подробности в теме «Правила».",
)

_ELEVATOR_REPLIES = (
    "С лифтом обычно так: если застряли — жмите кнопку вызова в кабине и звоните диспетчеру. "
    "Если просто не работает — сообщите в УК с номером подъезда.",
    "Лифт капризничает? Бывает. Номер подъезда, этаж, что именно не так — и сразу в УК. "
    "Если серьёзно застряли — кнопка вызова и 112.",
    "Когда лифт барахлит, лучше сразу зафиксировать: подъезд, время, что происходит. "
    "В УК реагируют быстрее, когда есть конкретика.",
)

_TRASH_REPLIES = (
    "По мусору: если контейнеры переполнены или площадка грязная — фото и в чат/УК. "
    "Крупногабарит вывозят по графику, уточняйте у УК.",
    "С мусором просто: переполнен бак — фото, дата, место, и в УК. "
    "Для крупного мусора обычно есть отдельный график вывоза.",
    "Мусорная тема? Фиксируйте: что именно не так, где, когда. Фото в помощь. "
    "УК обязана реагировать на обращения по чистоте территории.",
)

_UTILITY_REPLIES = (
    "По коммуналке: показания счётчиков обычно передают через личный кабинет или приложение УК. "
    "Перерасчёт — письменное заявление в УК с основанием.",
    "Коммунальные вопросы лучше решать через личный кабинет или напрямую с УК. "
    "Если есть расхождения в квитанции — запросите детализацию.",
    "С коммуналкой советую так: все платежи фиксируйте, показания передавайте вовремя. "
    "Если что-то не сходится — в УК за разъяснением.",
)

_NEIGHBOR_REPLIES = (
    "С соседями лучше начинать с разговора. Если не помогает — зафиксируйте проблему "
    "и обратитесь в УК или чат с описанием ситуации без эмоций.",
    "Соседский вопрос? Тут главное — без наездов. Опишите ситуацию спокойно, "
    "попробуйте поговорить. Если не выходит — в УК с фактами.",
    "Конфликт с соседями — штука неприятная. Начните с личного разговора. "
    "Не помогло — опишите ситуацию в чате или обратитесь в УК. Факты решают лучше эмоций.",
)

_SECURITY_REPLIES = (
    "По безопасности: видеонаблюдение, охрана, домофон — всё через УК. "
    "Если что-то подозрительное — сразу охране или 112.",
    "Вопрос безопасности? Подозрительные лица, сломанный домофон, проблемы с камерами — "
    "всё это к УК и охране. В экстренных случаях — 112.",
    "С безопасностью лучше не тянуть: если что-то срочное — 112. "
    "По домофону, камерам, охране — обращение в УК с деталями.",
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
    "Опишите вопрос одной-двумя фразами: что случилось, где (подъезд/двор) и какой нужен результат.",
    "Расскажите, что именно интересует — подскажу, куда лучше написать или как решить.",
    "О чём хотите спросить? Напишите пару слов — разберёмся вместе!",
    "Слушаю! Опишите коротко ситуацию, и я постараюсь помочь.",
    "Что случилось? Напишите кратко — подскажу, как лучше действовать.",
    "Привет! Задайте вопрос — помогу чем смогу 🏠",
    "Я весь внимание! Что у вас стряслось?",
    "Напишите, что вас интересует — попробую помочь или подсказать, куда обратиться.",
    "Готов помочь! Опишите проблему или вопрос в паре предложений.",
    "Здесь, слушаю. О чём хотите узнать?",
)

_GREETING_REPLIES = (
    "Привет! Жабот на связи, чем помочь? 🏠",
    "Здравствуйте! Жабот слушает, задавайте вопрос.",
    "Привет-привет! Жабот тут, что интересует?",
    "Добрый день! Жабот к вашим услугам.",
    "Приветствую! Жабот готов помочь по любому вопросу дома.",
    "Здравствуйте! Жабот рад вас видеть, спрашивайте.",
    "Привет! Жабот на посту, спрашивайте.",
    "О, привет! Жабот всегда рад помочь соседу!",
)

_THANKS_REPLIES = (
    "Жабот рад, что помог! Обращайтесь, если что 🙌",
    "На здоровье! Жабот всегда тут, если что-то ещё.",
    "Пожалуйста! Жабот всегда рад помочь.",
    "Не за что! Соседям помогать — удовольствие Жабота.",
    "Обращайтесь! Жабот желает удачи с вашим вопросом.",
    "Всегда пожалуйста! Жабот на связи 😊",
    "Рад был помочь! Хорошего дня от Жабота.",
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

    # FAQ-ответ — наивысший приоритет (закреплённый ответ из базы)
    if faq_hint:
        return faq_hint[:800]

    # Данные из БД инфраструктуры приоритетнее статичной базы знаний
    if places_hint:
        # Варьируем вступление к ответу из БД инфраструктуры
        intros = (
            "Вот что нашёл в базе инфраструктуры:",
            "По базе инфраструктуры нашлось:",
            "Есть информация по вашему запросу:",
            "Нашёл кое-что полезное:",
        )
        return f"{random.choice(intros)}\n{places_hint[:700]}"

    # RAG-контекст из базы знаний ЖК
    if rag_hint:
        intros = (
            "Вот что нашёл в базе знаний:",
            "По базе знаний есть такая информация:",
            "Нашёл в наших записях:",
            "Есть данные по этой теме:",
        )
        return f"{random.choice(intros)}\n{rag_hint[:700]}"

    resident_answer = build_resident_answer(normalized_prompt, context=context)
    if resident_answer:
        return resident_answer

    rule_reply = _assistant_rule_reply(normalized_prompt)
    if rule_reply:
        return rule_reply

    return _pick_fallback_variant(normalized_prompt)



def parse_quiz_answer_response(data: dict[str, object]) -> QuizAnswerDecision:
    is_correct = bool(data.get("is_correct", False))
    is_close = bool(data.get("is_close", False))
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    reason = str(data.get("reason", ""))[:300]
    if is_correct:
        is_close = True
    return QuizAnswerDecision(
        is_correct=is_correct,
        is_close=is_close,
        confidence=confidence,
        reason=reason,
        used_fallback=False,
    )

def _normalize_quiz_text(text: str) -> str:
    normalized = re.sub(r"[^\w\s]+", " ", text.lower().replace("ё", "е"))
    return " ".join(normalized.split())


def local_quiz_answer_decision(correct_answer: str, user_answer: str) -> QuizAnswerDecision:
    correct = _normalize_quiz_text(correct_answer)
    answer = _normalize_quiz_text(user_answer)
    if not correct or not answer:
        return QuizAnswerDecision(False, False, 0.0, "пустой ответ", False)

    if correct == answer:
        return QuizAnswerDecision(True, True, 0.95, "точное совпадение", False)

    correct_words = set(correct.split())
    answer_words = set(answer.split())
    overlap = len(correct_words & answer_words)
    if not correct_words:
        return QuizAnswerDecision(False, False, 0.0, "нет эталона", False)

    ratio = overlap / len(correct_words)
    if ratio >= 0.8:
        return QuizAnswerDecision(True, True, 0.8, "почти полный смысловой матч", False)
    if ratio >= 0.3:
        return QuizAnswerDecision(False, True, 0.6, "частично близкий ответ", False)
    return QuizAnswerDecision(False, False, 0.2, "не совпадает", False)


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
_LAST_ERROR: str | None = "stub_mode"
_LAST_ERROR_AT: datetime | None = datetime.utcnow()


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
    return AiRuntimeStatus(last_error=_LAST_ERROR, last_error_at=_LAST_ERROR_AT)


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
        _LAST_ERROR_AT = datetime.utcnow()
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
            _LAST_ERROR_AT = datetime.utcnow()
    return _AI_CLIENT


async def close_ai_client() -> None:
    global _AI_CLIENT
    if _AI_CLIENT is None:
        return
    await _AI_CLIENT.aclose()
    _AI_CLIENT = None
