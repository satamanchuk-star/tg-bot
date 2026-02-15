"""Почему: сохраняем точки расширения для ИИ, но держим бота в безопасном локальном режиме."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Literal, Protocol

import httpx

from app.config import settings
from app.db import get_session
from app.services.ai_usage import add_usage, can_consume_ai, get_usage_stats
from app.utils.time import now_tz

logger = logging.getLogger(__name__)

_MODERATION_SYSTEM_PROMPT = (
    "Верни только JSON без дополнительного текста: "
    '{"violation_type":"none|profanity|rude|aggression","severity":0-3,'
    '"confidence":0..1,"action":"none|warn|delete_warn|delete_strike"}. '
    "Правила: учитывай замаскированный мат, оскорбления и агрессию; "
    "не выдумывай факты; при сомнении выбирай более мягкое действие."
)

_ASSISTANT_SYSTEM_PROMPT = (
    "Ты виртуальный сосед-помощник в чате ЖК. Отвечай так, как живой человек: "
    "по-человечески, с эмпатией, без канцелярита и без упоминания, что ты ИИ. "
    "Пиши на русском, кратко (до 800 символов, обычно 2-5 предложений), "
    "можно 1 уместный эмодзи, без таблиц и длинных списков. "
    "Базовые ограничения: не помогай с политикой, религией, нацконфликтами, "
    "медицинскими назначениями, юридическими консультациями, финансовыми советами, "
    "сбором персональных данных. Если запрос вне рамок — вежливо откажи и предложи "
    "безопасную альтернативу по теме ЖК/быта."
)

_DAILY_SUMMARY_SYSTEM_PROMPT = (
    "Сформируй краткую сводку для админов чата ЖК на русском: до 800 символов, "
    "без таблиц, без персональных данных, нейтрально и по фактам."
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
    "заткни",
    "идиот",
    "туп",
    "дебил",
    "ненавиж",
    "пошел",
    "отвали",
    "замолчи",
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
_DIGIT_TO_CYR = str.maketrans({"0": "о", "3": "з", "4": "ч", "6": "б"})

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


class AiProvider(Protocol):
    async def probe(self) -> AiProbeResult: ...

    async def moderate(self, text: str, *, chat_id: int) -> ModerationDecision: ...

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


class StubAiProvider:
    """Почему: стабильно возвращает локальное поведение до реального подключения ИИ."""

    async def probe(self) -> AiProbeResult:
        return AiProbeResult(False, "ИИ отключен: используется stub-провайдер.", 0)

    async def moderate(self, text: str, *, chat_id: int) -> ModerationDecision:
        decision = local_moderation(text)
        decision.used_fallback = True
        return decision

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return "С этим лучше к профильному специалисту 🙌 Я тут больше про жизнь дома."
        return f"{_USER_FALLBACK} {build_local_assistant_reply(safe_prompt)}"

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
            "temperature": 0.2,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {settings.ai_key}",
            "Content-Type": "application/json",
        }

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

    async def moderate(self, text: str, *, chat_id: int) -> ModerationDecision:
        try:
            content, _ = await self._chat_completion(
                [
                    {"role": "system", "content": _MODERATION_SYSTEM_PROMPT},
                    {"role": "user", "content": text[:2000]},
                ],
                chat_id=chat_id,
            )
            data = json.loads(content)
            violation_type = str(data.get("violation_type", "none"))
            action = str(data.get("action", "none"))
            severity = int(data.get("severity", 0))
            confidence = float(data.get("confidence", 0.5))
            if violation_type not in {"none", "profanity", "rude", "aggression"}:
                violation_type = "none"
            if action not in {"none", "warn", "delete_warn", "delete_strike"}:
                action = "none"
            severity = max(0, min(3, severity))
            confidence = max(0.0, min(1.0, confidence))
            return ModerationDecision(violation_type, severity, confidence, action, False)
        except (RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record_runtime_error(exc)
            decision = local_moderation(text)
            decision.used_fallback = True
            return decision

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return "С этим лучше к профильному специалисту 🙌 Я тут больше про жизнь дома."
        context_text = "\n".join(context[-20:])
        try:
            content, _ = await self._chat_completion(
                [
                    {"role": "system", "content": _ASSISTANT_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Контекст:\n{context_text}\n\nВопрос:\n{safe_prompt}"},
                ],
                chat_id=chat_id,
            )
            return content[:800]
        except RuntimeError as exc:
            self._record_runtime_error(exc)
            return build_local_assistant_reply(safe_prompt)

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

    async def moderate(self, text: str, *, chat_id: int) -> ModerationDecision:
        return await self._provider.moderate(text, chat_id=chat_id)

    async def assistant_reply(self, prompt: str, context: list[str], *, chat_id: int) -> str:
        return await self._provider.assistant_reply(prompt, context, chat_id=chat_id)

    async def evaluate_quiz_answer(
        self,
        question: str,
        correct_answer: str,
        user_answer: str,
        *,
        chat_id: int,
    ) -> QuizAnswerDecision:
        return await self._provider.evaluate_quiz_answer(
            question,
            correct_answer,
            user_answer,
            chat_id=chat_id,
        )

    async def generate_daily_summary(self, context: str, *, chat_id: int) -> str | None:
        return await self._provider.generate_daily_summary(context, chat_id=chat_id)


def local_moderation(text: str) -> ModerationDecision:
    normalized = normalize_for_profanity(text)
    if detect_profanity(normalized):
        return ModerationDecision("profanity", 3, 0.95, "delete_strike", False)
    lowered = text.lower()
    if any(pattern in lowered for pattern in _RUDE_PATTERNS):
        return ModerationDecision("rude", 1, 0.8, "warn", False)
    return ModerationDecision("none", 0, 0.99, "none", False)


def normalize_for_profanity(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    lowered = lowered.translate(_LATIN_TO_CYR).translate(_DIGIT_TO_CYR)
    lowered = re.sub(r"[\s\-_.*/]+", "", lowered)
    return lowered


def detect_profanity(normalized: str) -> bool:
    roots = ("хуй", "пизд", "еб", "бля", "сук", "муд", "гандон")
    return any(root in normalized for root in roots)


def mask_personal_data(text: str) -> str:
    text = PHONE_RE.sub("[скрыт_телефон]", text)
    text = EMAIL_RE.sub("[скрыт_email]", text)
    return FULLNAME_RE.sub("[скрыто_фио]", text)


def is_assistant_topic_allowed(text: str) -> bool:
    lowered = text.lower()
    if any(token in lowered for token in _FORBIDDEN_ASSISTANT_TOPICS):
        return False
    return any(token in lowered for token in _ALLOWED_ASSISTANT_TOPICS)


def build_local_assistant_reply(prompt: str) -> str:
    if "шлагбаум" in prompt.lower():
        return "По шлагбауму лучше писать в профильную тему. Накиньте номер авто и суть, помогу собрать короткий текст."
    return "Хороший вопрос. Добавьте адрес или подъезд и что хотите получить на выходе — так быстрее подскажут."



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
