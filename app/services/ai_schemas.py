"""Pydantic-схемы для структурированных AI-ответов классификации."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModerationResult(BaseModel):
    is_violation: bool = False
    has_profanity: bool = False
    is_toxic: bool = False
    is_threat: bool = False
    category: str = "none"
    confidence: float = 0.0
    recommended_action: str = "log_only"  # log_only | warn | delete_and_strike
    reason: str = ""


class SpamResult(BaseModel):
    is_spam: bool = False
    has_external_link: bool = False
    category: str = "none"
    confidence: float = 0.0
    recommended_action: str = "log_only"  # log_only | delete
    reason: str = ""


class TopicResult(BaseModel):
    topic_key: str = ""
    topic_title: str = ""
    confidence: float = 0.0
    suggestion_text: str = ""


class GateIntentResult(BaseModel):
    is_gate_problem: bool = False
    confidence: float = 0.0
    reason: str = ""


class GateRequestResult(BaseModel):
    date_time: str | None = None
    car_number: str | None = None
    in_pass_base: str | None = None
    problem_description: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = 0.0
