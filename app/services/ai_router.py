"""Маршрутизация AI-задач: какая модель, температура и лимит токенов для каждой задачи."""

from __future__ import annotations

from app.config import settings

_TASK_CONFIG: dict[str, dict[str, object]] = {
    "moderation":    {"model": "ai_classifier_model",    "temp": 0.0, "tokens": "ai_classifier_max_output_tokens"},
    "spam":          {"model": "ai_spam_model",           "temp": 0.0, "tokens": "ai_classifier_max_output_tokens"},
    "topic":         {"model": "ai_topic_model",          "temp": 0.0, "tokens": "ai_classifier_max_output_tokens"},
    "gate_intent":   {"model": "ai_gate_intent_model",    "temp": 0.0, "tokens": "ai_classifier_max_output_tokens"},
    "gate_extract":  {"model": "ai_gate_extract_model",   "temp": 0.1, "tokens": "ai_reply_max_output_tokens"},
    "faq":           {"model": "ai_faq_model",            "temp": 0.3, "tokens": "ai_reply_max_output_tokens"},
    "reply":         {"model": "ai_reply_model",          "temp": 0.7, "tokens": "ai_reply_max_output_tokens"},
    "digest":        {"model": "ai_digest_model",         "temp": 0.8, "tokens": "ai_digest_max_output_tokens"},
    "premium_reply": {"model": "ai_premium_model",        "temp": 0.5, "tokens": "ai_reply_max_output_tokens"},
    "code_help":     {"model": "ai_code_model",           "temp": 0.2, "tokens": "ai_reply_max_output_tokens"},
    "image":         {"model": "ai_image_model",          "temp": 1.0, "tokens": None},
}


def get_model_for_task(task: str) -> str:
    cfg = _TASK_CONFIG.get(task)
    if cfg is None:
        return settings.ai_fallback_model
    field = str(cfg["model"])
    return str(getattr(settings, field, settings.ai_fallback_model))


def get_temperature_for_task(task: str) -> float:
    cfg = _TASK_CONFIG.get(task)
    return float(cfg["temp"]) if cfg else 0.7


def get_max_tokens_for_task(task: str) -> int:
    cfg = _TASK_CONFIG.get(task)
    if cfg is None:
        return 500
    field = cfg.get("tokens")
    if field is None:
        return 1024
    return int(getattr(settings, str(field), 500))
