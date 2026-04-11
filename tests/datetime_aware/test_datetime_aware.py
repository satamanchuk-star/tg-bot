"""Почему: предотвращаем регрессии с naive/aware datetime в викторине."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import QuizSession
from app.services.quiz import QUIZ_STALE_SESSION_SEC, _is_session_stale
from app.utils.time import ensure_aware


def test_ensure_aware_adds_utc_to_naive() -> None:
    naive = datetime(2024, 1, 1, 12, 0, 0)
    aware = ensure_aware(naive)
    assert aware.tzinfo == timezone.utc


def test_ensure_aware_keeps_aware() -> None:
    aware_dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ensure_aware(aware_dt) == aware_dt


def test_is_session_stale_with_naive_datetime() -> None:
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=1)
    session.question_started_at = datetime(2024, 1, 1, 12, 0, 0)
    assert _is_session_stale(session) is True


def test_is_session_stale_none_started_at_after_questions() -> None:
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=3)
    session.question_started_at = None
    assert _is_session_stale(session) is True


def test_is_session_stale_none_started_at_initial_session() -> None:
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=0)
    session.question_started_at = None
    assert _is_session_stale(session) is False


def test_is_session_stale_old_aware_datetime() -> None:
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=1)
    session.question_started_at = datetime.now(timezone.utc) - timedelta(seconds=QUIZ_STALE_SESSION_SEC + 60)
    assert _is_session_stale(session) is True
