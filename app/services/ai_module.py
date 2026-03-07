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
    "ГЛАВНОЕ ПРАВИЛО: анализируй КОНТЕКСТ и НАМЕРЕНИЕ сообщения, а не отдельные слова.\n"
    "Если перед сообщением приведён контекст беседы — используй его для оценки тона.\n"
    "Дружеская перепалка (взаимные ответы с шутками/смайлами) — НЕ нарушение.\n"
    "Матерные и грубые слова в дружеском или нейтральном контексте — НЕ нарушение.\n"
    "Например: «блин, опять лифт сломался» или «ну нифига себе цены» — это severity 0.\n"
    "Лёгкая грубость в бытовом общении между соседями — НЕ повод для наказания.\n\n"
    "Удаление и бан ТОЛЬКО за:\n"
    "- Прямые оскорбления конкретного человека с агрессией (severity 3)\n"
    "- Угрозы физической расправой (severity 3)\n"
    "- Доксинг — публикация чужих персональных данных (severity 3)\n"
    "- Целенаправленная травля или буллинг (severity 3)\n"
    "- Спам и реклама (severity 2)\n\n"
    "НЕ наказывай за:\n"
    "- Мат без агрессии и без адресата (бытовой мат): severity 0\n"
    "- Эмоциональные высказывания без оскорблений конкретных людей: severity 0\n"
    "- Жалобы на соседей, УК, сервисы (даже в грубой форме): severity 0\n"
    "- Сарказм и ирония: severity 0\n"
    "- Грубоватый юмор: severity 0\n\n"
    "Поле sentiment: оцени общий тон сообщения (positive/neutral/negative).\n"
    "При ЛЮБОМ сомнении — severity 0 (не наказывать). "
    "Лучше пропустить 10 грубых сообщений, чем наказать 1 невиновного."
)

_ASSISTANT_SYSTEM_PROMPT = (
    "Ты дружелюбный сосед-помощник в чате ЖК. Отвечай так, как живой человек: "
    "по-человечески, с эмпатией, без канцелярита и без упоминания, что ты ИИ. "
    "Пиши на русском, кратко (до 800 символов, обычно 2-5 предложений), "
    "можно 1 уместный эмодзи, без таблиц и длинных списков. "
    "Базовые ограничения: не помогай с политикой, религией, нацконфликтами, "
    "медицинскими назначениями, юридическими консультациями, финансовыми советами, "
    "сбором персональных данных. Если запрос вне рамок — вежливо откажи и предложи "
    "безопасную альтернативу по теме ЖК/быта.\n\n"
    "СТИЛЬ ОБЩЕНИЯ: каждый раз формулируй ответ по-новому, даже если вопрос повторяется. "
    "Варьируй порядок подачи информации, меняй вступление и заключение. "
    "Чередуй тон: иногда дружеский, иногда деловой, иногда с лёгким юмором. "
    "Можешь добавить уместную бытовую деталь из жизни дома. "
    "Никогда не начинай ответ одинаково — избегай шаблонных фраз-открытий.\n\n"
    "ВАЖНО: если в контексте есть раздел «Каноническая база знаний ЖК», используй его "
    "в первую очередь. Только потом учитывай раздел «База знаний ЖК» из RAG. "
    "Если есть «Рекомендуемый ответ из FAQ», передай ту же суть, но своими словами. "
    "Категории базы знаний могут включать парковку, лифт, УК, "
    "коммуналку, безопасность, детскую площадку, коммунальные сервисы, "
    "безопасность и доступ, платежи, ремонт, правила и общее. "
    "В базе могут быть дубли и фрагменты одной темы — "
    "объединяй их по смыслу и отвечай только нужной пользователю выжимкой. "
    "Не перечисляй лишние детали. Если точной информации нет, честно скажи об этом "
    "и предложи следующий шаг в добродушном, лёгком и немного шутливом тоне "
    "без выдумывания фактов. Если пользователь выражается резко, вежливо напомни, "
    "что в чате поддерживается дружелюбная атмосфера, и помоги перевести разговор "
    "в конструктивный тон."
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

_USER_FALLBACK = "Модуль ИИ в подготовке, работает локальный режим."

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
    "медицин",
    "диагноз",
    "юрид",
    "суд",
    "адвокат",
    "финанс",
    "инвест",
    "кредит",
    "паспорт",
    "телефон",
    "email",
)
_RUDE_PATTERNS = (
    "убью",
    "убить",
    "сдохни",
    "уничтож",
    "калечить",
)
_FORBIDDEN_TOPIC_REPLIES = (
    "С этим лучше к профильному специалисту 🙌 Я тут больше про жизнь дома.",
    "Это не совсем мой профиль — я больше по домашним вопросам. Лучше спросить у специалиста.",
    "Тут я пас, не хочу давать непрофессиональный совет. Обратитесь к профильному эксперту!",
    "По такому вопросу лучше к специалисту. Я больше по вопросам нашего ЖК 🏠",
    "Здесь я не помощник — это вне моей зоны. Но по дому спрашивайте смело!",
)

_AGGRESSIVE_INSULT_PATTERNS = (
    "идиот",
    "дебил",
    "даун",
    "уродин",
    "мразь",
    "тварь",
    "ублюд",
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
        return f"{_USER_FALLBACK} {build_local_assistant_reply(safe_prompt, context=context)}"

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
        self._model = settings.ai_model
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
            "model": self._model,
            "temperature": 0.7,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {settings.ai_key}",
            "Content-Type": "application/json",
        }
        logger.info("AI request -> model=%s chat_id=%s", self._model, chat_id)

        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post("/chat/completions", json=payload, headers=headers)
                if response.status_code >= 500 and attempt < self._retries:
                    continue
                response.raise_for_status()
                data = response.json()
                content = str(data["choices"][0]["message"]["content"])
                tokens = int(data.get("usage", {}).get("total_tokens") or 0)
                await _add_remote_usage(chat_id, tokens)
                logger.info("AI response <- tokens=%s chat_id=%s", tokens, chat_id)
                return content, tokens
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
                user_content += "\n".join(context[-5:]) + "\n\n"
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

        rag_text = await _get_rag_context(chat_id, safe_prompt)
        faq_answer = await _get_faq_answer(chat_id, safe_prompt)

        system_prompt = _ASSISTANT_SYSTEM_PROMPT
        resident_context = build_resident_context(safe_prompt, context=context)
        if resident_context:
            system_prompt += f"\n\nКаноническая база знаний ЖК:\n{resident_context}"
        if rag_text:
            system_prompt += f"\n\nБаза знаний ЖК:\n{rag_text}"
        if faq_answer:
            system_prompt += f"\n\nРекомендуемый ответ из FAQ (перефразируй, не копируй дословно):\n{faq_answer}"

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
            return build_local_assistant_reply(safe_prompt, context=context)

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
            return f"{_USER_FALLBACK} {build_local_assistant_reply(prompt, context=context)}"

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
    """Проверяет, направлена ли грубость на конкретного человека."""
    lowered = text.lower()
    target_markers = ("ты ", "тебя ", "тебе ", "вы ", "вас ", "вам ", "@")
    return any(marker in lowered for marker in target_markers)


def local_moderation(text: str) -> ModerationDecision:
    normalized = normalize_for_profanity(text)
    lowered = text.lower()
    aggression_level = detect_aggression_level(text)

    # Угрозы физической расправой — всегда severity 3
    if any(pattern in lowered for pattern in _RUDE_PATTERNS):
        return ModerationDecision("aggression", 3, 0.9, "delete_strike", False)

    has_profanity = detect_profanity(normalized)
    has_insult = any(pattern in lowered for pattern in _AGGRESSIVE_INSULT_PATTERNS)

    # Прямое оскорбление конкретного человека с матом — severity 3
    if has_profanity and has_insult and _has_aggressive_target(text):
        return ModerationDecision("aggression", 3, 0.85, "delete_strike", False)

    # Агрессия средней силы — предупреждение без удаления
    if has_profanity and _has_aggressive_target(text) and aggression_level == "low":
        return ModerationDecision("profanity", 1, 0.7, "warn", False)

    # Мат без агрессии и адресата (бытовой мат) — severity 0, не наказываем
    if has_profanity:
        return ModerationDecision("none", 0, 0.6, "none", False)

    # Оскорбление без мата, направленное на человека — severity 1
    if has_insult and _has_aggressive_target(text):
        return ModerationDecision("rude", 1, 0.7, "warn", False)

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
    has_target = _has_aggressive_target(text)
    has_profanity = detect_profanity(normalize_for_profanity(text))

    if has_threat or (has_insult and has_target and has_profanity):
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


def _assistant_rule_reply(prompt: str) -> str | None:
    lowered = prompt.lower()
    rules_keywords = ("правил", "нельзя", "запрещ", "можно ли", "регламент")
    gate_keywords = ("шлагбаум", "пропуск", "въезд", "проезд", "пульт", "ворота")
    complaint_keywords = ("жалоб", "претенз", "не работает", "слом", "гряз", "протеч")
    noise_keywords = ("шум", "тих", "громк", "ноч", "ремонт")
    parking_keywords = ("парков", "машин", "авто", "место")

    if any(keyword in lowered for keyword in gate_keywords):
        return random.choice(_GATE_REPLIES)
    if any(keyword in lowered for keyword in noise_keywords):
        return random.choice(_NOISE_REPLIES)
    if any(keyword in lowered for keyword in complaint_keywords):
        return random.choice(_COMPLAINT_REPLIES)
    if any(keyword in lowered for keyword in parking_keywords):
        return random.choice(_PARKING_REPLIES)
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
)


def build_local_assistant_reply(prompt: str, *, context: list[str] | None = None) -> str:
    normalized_prompt = _normalize_assistant_prompt(prompt)
    if not normalized_prompt:
        return random.choice(_EMPTY_PROMPT_REPLIES)

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
_AI_RUNTIME_ENABLED: bool = False
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


async def get_ai_usage_for_today(chat_id: int) -> tuple[int, int]:
    date_key = now_tz().date().isoformat()
    async for session in get_session():
        usage = await get_usage_stats(session, date_key=date_key, chat_id=chat_id)
        return usage.requests_used, usage.tokens_used
    return 0, 0


async def get_ai_diagnostics(chat_id: int) -> AiDiagnosticsReport:
    provider_mode: Literal["remote", "stub"] = "remote" if settings.ai_enabled and bool(settings.ai_key) else "stub"
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
    global _AI_RUNTIME_ENABLED
    _AI_RUNTIME_ENABLED = value
    logger.info("AI runtime toggle requested (%s), но активен stub-режим.", value)


def get_ai_client() -> AiModuleClient:
    global _LAST_ERROR, _LAST_ERROR_AT
    global _AI_CLIENT
    if _AI_CLIENT is None:
        if settings.ai_enabled and settings.ai_key:
            _AI_CLIENT = AiModuleClient(OpenRouterProvider())
            _LAST_ERROR = None
            _LAST_ERROR_AT = None
        else:
            _AI_CLIENT = AiModuleClient()
            _LAST_ERROR = "stub_mode"
            _LAST_ERROR_AT = datetime.utcnow()
    return _AI_CLIENT


async def close_ai_client() -> None:
    global _AI_CLIENT
    if _AI_CLIENT is None:
        return
    await _AI_CLIENT.aclose()
    _AI_CLIENT = None
