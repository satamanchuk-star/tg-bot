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


class AiModuleClient:
    def __init__(self) -> None:
        timeout = httpx.Timeout(settings.ai_timeout_seconds)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def moderate(self, text: str) -> ModerationDecision:
        local_decision = local_moderation(text)
        if not settings.ai_enabled or not settings.ai_api_url:
            return local_decision

        payload = {
            "mode": "moderation",
            "text": text,
            "language": "ru",
            "policy": "severity_0_3",
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
            return "Извините, с этой темой я не могу помочь. Могу подсказать по вопросам ЖК и бытовым темам."

        if not settings.ai_enabled or not settings.ai_api_url:
            return build_local_assistant_reply(safe_prompt)

        payload = {
            "mode": "assistant",
            "language": "ru",
            "style": "brief_friendly",
            "max_chars": 800,
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
                return "Сейчас не могу ответить подробно. Уточните вопрос, и я помогу."
            return text[:800]
        except (httpx.HTTPError, json.JSONDecodeError):
            logger.warning("AI assistant недоступен, используем локальный ответ.")
            return build_local_assistant_reply(safe_prompt)


def parse_moderation_response(data: dict[str, object]) -> ModerationDecision:
    violation_type = str(data.get("violation_type", "none"))
    severity = int(data.get("severity", 0))
    confidence = float(data.get("confidence", 0.5))
    action = str(data.get("action", "none"))
    if violation_type not in {"none", "profanity", "rude", "aggression"}:
        violation_type = "none"
    if action not in {"none", "warn", "delete_warn", "delete_strike"}:
        action = map_action_by_severity(severity)
    severity = max(0, min(3, severity))
    confidence = max(0.0, min(1.0, confidence))
    return ModerationDecision(
        violation_type=violation_type,
        severity=severity,
        confidence=confidence,
        action=action,
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
        return "По шлагбауму лучше писать в профильный топик. Укажите номер авто и суть проблемы, помогу сформулировать коротко."
    return "Понял вопрос. Лучше уточнить адрес/подъезд и желаемый результат — так соседи и админы быстрее помогут."

_AI_CLIENT: AiModuleClient | None = None


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
