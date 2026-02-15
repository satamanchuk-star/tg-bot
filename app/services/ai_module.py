"""Почему: инкапсулируем ИИ-политику, чтобы модерация и ассистент были предсказуемы и тестируемы."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MODERATION_SYSTEM_PROMPT = """Ты — модератор чата жилого комплекса.\n\nТвоя задача — анализировать сообщения и определять:\n- наличие мата (включая замаскированный),\n- грубость,\n- агрессию,\n- угрозы.\n\nТы возвращаешь только JSON без пояснений.\n\nУчитывай замены букв символами, пробелы между буквами, латиницу вместо кириллицы, цифры, транслитерацию и частично скрытые слова.\nМат считается нарушением даже если он замаскирован.\n\nУровни severity:\n0 — нет нарушения\n1 — мягкая грубость\n2 — явное нарушение\n3 — серьёзное нарушение\n\nФормат ответа:\n{\n  "label": "PROFANITY|RUDE|HATE|THREAT|NONE",\n  "severity": 0,\n  "confidence": 0.0,\n  "recommended_action": "ALLOW|WARN|DELETE|STRIKE|ADMIN_ALERT",\n  "user_message": "короткая живая фраза",\n  "admin_note": "краткое пояснение для админов"\n}\n\nПравила:\n- Никакого текста вне JSON.\n- user_message до 200 символов.\n- Спокойный живой тон без канцелярита.\n- Не упоминать алгоритмы, ИИ или систему.\n"""

ASSISTANT_SYSTEM_PROMPT = """Ты — участник чата жилого комплекса.\nОтвечай как живой человек: коротко, дружелюбно, спокойно, по делу.\nДопускается лёгкий нейтральный юмор без сарказма.\n\nЗапрещено: \"как ИИ\", упоминания алгоритмов и автоматической модерации, канцелярит, моральный тон.\nНе давай медицинские, юридические и финансовые советы, не обсуждай политику и религию.\nЕсли тема вне зоны — мягко откажи: \"С этим лучше к профильному специалисту 🙌 Я тут больше про жизнь дома.\"\n\nОграничения: максимум 800 символов, без таблиц и длинных абзацев.\nЕсли есть конфликт, мягко деэскалируй: \"Можно спорить, но спокойно.\"\n"""

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


class AiModuleClient:
    def __init__(self) -> None:
        timeout = httpx.Timeout(settings.ai_timeout_seconds)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe(self) -> AiProbeResult:
        """Выполняет минимальный запрос к AI endpoint и возвращает человекочитаемый статус."""
        if not settings.ai_api_url:
            return AiProbeResult(False, "Не задан AI_API_URL.")
        if not settings.ai_key:
            return AiProbeResult(False, "Не задан AI_KEY.")

        payload = {
            "mode": "moderation",
            "text": "тест",
            "language": "ru",
            "policy": "severity_0_3",
            "system_prompt": MODERATION_SYSTEM_PROMPT,
        }
        headers = {"Authorization": f"Bearer {settings.ai_key}"}

        try:
            response = await self._client.post(settings.ai_api_url, json=payload, headers=headers)
        except httpx.TimeoutException:
            return AiProbeResult(False, "Timeout: нет ответа от AI endpoint.")
        except httpx.HTTPError as exc:
            return AiProbeResult(False, f"Сетевая ошибка: {exc.__class__.__name__}.")

        if response.status_code == 401:
            return AiProbeResult(False, "401 Unauthorized: ключ невалиден или не передан.")
        if response.status_code == 403:
            return AiProbeResult(False, "403 Forbidden: нет доступа к модели или endpoint.")
        if response.status_code == 404:
            return AiProbeResult(False, "404 Not Found: проверь AI_API_URL.")
        if response.status_code >= 400:
            return AiProbeResult(False, f"HTTP {response.status_code}: {response.text[:120]}")

        try:
            data = response.json()
            parse_moderation_response(data)
        except (json.JSONDecodeError, ValueError, KeyError):
            return AiProbeResult(False, "Ответ получен, но формат JSON не распознан для режима moderation.")
        return AiProbeResult(True, "AI endpoint ответил корректно.")

    async def moderate(self, text: str) -> ModerationDecision:
        local_decision = local_moderation(text)
        if not is_ai_runtime_enabled() or not settings.ai_api_url:
            return local_decision

        payload = {
            "mode": "moderation",
            "text": text,
            "language": "ru",
            "policy": "severity_0_3",
            "system_prompt": MODERATION_SYSTEM_PROMPT,
        }
        headers = {"Authorization": f"Bearer {settings.ai_key}"} if settings.ai_key else {}

        for attempt in range(settings.ai_retries + 1):
            try:
                response = await self._client.post(settings.ai_api_url, json=payload, headers=headers)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError("5xx", request=response.request, response=response)
                response.raise_for_status()
                data = response.json()
                return parse_moderation_response(data)
            except (httpx.TimeoutException, httpx.HTTPStatusError, json.JSONDecodeError, KeyError, ValueError):
                if attempt >= settings.ai_retries:
                    logger.warning("AI moderation недоступна, используем локальный фильтр.")
                    return ModerationDecision(
                        violation_type=local_decision.violation_type,
                        severity=local_decision.severity,
                        confidence=local_decision.confidence,
                        action=local_decision.action,
                        used_fallback=True,
                    )
        return local_decision

    async def assistant_reply(self, prompt: str, context: list[str]) -> str:
        safe_prompt = mask_personal_data(prompt)[:1000]
        if not is_assistant_topic_allowed(safe_prompt):
            return "С этим лучше к профильному специалисту 🙌 Я тут больше про жизнь дома."

        if not is_ai_runtime_enabled() or not settings.ai_api_url:
            return build_local_assistant_reply(safe_prompt)

        payload = {
            "mode": "assistant",
            "language": "ru",
            "style": "brief_friendly_human",
            "max_chars": 800,
            "system_prompt": ASSISTANT_SYSTEM_PROMPT,
            "prompt": safe_prompt,
            "context": [mask_personal_data(item) for item in context[-20:]],
        }
        headers = {"Authorization": f"Bearer {settings.ai_key}"} if settings.ai_key else {}

        try:
            response = await self._client.post(settings.ai_api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            text = str(data.get("reply", "")).strip()
            if not text:
                return "Сейчас не сориентирую сразу. Дайте чуть больше деталей, и разберём."
            return text[:800]
        except (httpx.HTTPError, json.JSONDecodeError):
            logger.warning("AI assistant недоступен, используем локальный ответ.")
            return build_local_assistant_reply(safe_prompt)

    async def evaluate_quiz_answer(self, question: str, correct_answer: str, user_answer: str) -> QuizAnswerDecision:
        if not is_ai_runtime_enabled() or not settings.ai_api_url:
            return local_quiz_answer_decision(correct_answer, user_answer)

        payload = {
            "mode": "quiz_judge",
            "language": "ru",
            "question": question[:1200],
            "correct_answer": correct_answer[:400],
            "user_answer": user_answer[:400],
            "policy": "contextual_equivalence_with_close_answers",
        }
        headers = {"Authorization": f"Bearer {settings.ai_key}"} if settings.ai_key else {}

        try:
            response = await self._client.post(settings.ai_api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            return parse_quiz_answer_response(data)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError):
            logger.warning("AI оценка викторины недоступна, используем локальную эвристику.")
            decision = local_quiz_answer_decision(correct_answer, user_answer)
            return QuizAnswerDecision(
                is_correct=decision.is_correct,
                is_close=decision.is_close,
                confidence=decision.confidence,
                reason=decision.reason,
                used_fallback=True,
            )


def parse_moderation_response(data: dict[str, object]) -> ModerationDecision:
    raw_label = str(data.get("violation_type", data.get("label", "none"))).lower()
    violation_map = {
        "none": "none",
        "profanity": "profanity",
        "rude": "rude",
        "hate": "aggression",
        "threat": "aggression",
        "aggression": "aggression",
    }
    violation_type = violation_map.get(raw_label, "none")

    severity = int(data.get("severity", 0))
    confidence = float(data.get("confidence", 0.5))

    raw_action = str(data.get("action", data.get("recommended_action", "none"))).lower()
    action_map = {
        "none": "none",
        "allow": "none",
        "warn": "warn",
        "delete": "delete_warn",
        "delete_warn": "delete_warn",
        "strike": "delete_strike",
        "delete_strike": "delete_strike",
        "admin_alert": "delete_strike",
    }
    action = action_map.get(raw_action, map_action_by_severity(severity))

    severity = max(0, min(3, severity))
    confidence = max(0.0, min(1.0, confidence))
    return ModerationDecision(
        violation_type=violation_type,
        severity=severity,
        confidence=confidence,
        action=action,
        used_fallback=False,
    )


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


def map_action_by_severity(severity: int) -> Literal["none", "warn", "delete_warn", "delete_strike"]:
    return {
        0: "none",
        1: "warn",
        2: "delete_warn",
        3: "delete_strike",
    }.get(severity, "none")


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
_AI_RUNTIME_ENABLED: bool | None = None


def is_ai_runtime_enabled() -> bool:
    if _AI_RUNTIME_ENABLED is None:
        return settings.ai_enabled
    return _AI_RUNTIME_ENABLED


def set_ai_runtime_enabled(value: bool) -> None:
    global _AI_RUNTIME_ENABLED
    _AI_RUNTIME_ENABLED = value


def get_ai_client() -> AiModuleClient:
    global _AI_CLIENT
    if _AI_CLIENT is None:
        _AI_CLIENT = AiModuleClient()
    return _AI_CLIENT


async def close_ai_client() -> None:
    global _AI_CLIENT
    if _AI_CLIENT is None:
        return
    await _AI_CLIENT.aclose()
    _AI_CLIENT = None
