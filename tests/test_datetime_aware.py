"""Почему: гарантируем что _is_session_stale не бросает TypeError при naive datetime из SQLite."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import QuizSession
from app.services.quiz import _is_session_stale
from app.utils.time import ensure_aware


# ---------------------------------------------------------------------------
# Тесты ensure_aware()
# ---------------------------------------------------------------------------

def test_ensure_aware_adds_utc_to_naive() -> None:
    """ensure_aware() добавляет UTC tzinfo к naive datetime."""
    naive = datetime(2024, 1, 1, 12, 0, 0)
    aware = ensure_aware(naive)
    assert aware.tzinfo is not None
    assert aware.tzinfo == timezone.utc


def test_ensure_aware_keeps_aware_unchanged() -> None:
    """ensure_aware() не меняет уже aware datetime."""
    aware_dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = ensure_aware(aware_dt)
    assert result is aware_dt or result == aware_dt


def test_ensure_aware_non_utc_tzinfo() -> None:
    """ensure_aware() не трогает datetime с не-UTC tzinfo."""
    from zoneinfo import ZoneInfo
    moscow_tz = ZoneInfo("Europe/Moscow")
    moscow_dt = datetime(2024, 6, 15, 15, 0, 0, tzinfo=moscow_tz)
    result = ensure_aware(moscow_dt)
    assert result.tzinfo is not None
    assert result == moscow_dt


# ---------------------------------------------------------------------------
# Тесты _is_session_stale() с naive datetime (имитация SQLite)
# ---------------------------------------------------------------------------

def test_is_session_stale_with_naive_datetime() -> None:
    """_is_session_stale не бросает TypeError при naive datetime из SQLite."""
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=1)
    # Имитируем, что SQLite вернул naive datetime (без tzinfo) — очень старую дату
    session.question_started_at = datetime(2024, 1, 1, 12, 0, 0)  # naive, старая

    # Не должно бросить TypeError
    try:
        result = _is_session_stale(session)
    except TypeError as e:
        raise AssertionError(f"_is_session_stale бросил TypeError: {e}") from e

    # Очень старая сессия — должна считаться зависшей
    assert result is True


def test_is_session_stale_with_aware_datetime() -> None:
    """_is_session_stale корректно работает с aware datetime."""
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=1)
    # Текущее UTC время — сессия не зависшая
    session.question_started_at = datetime.now(timezone.utc)

    result = _is_session_stale(session)
    assert result is False


def test_is_session_stale_with_old_aware_datetime() -> None:
    """Старая aware datetime → сессия зависшая."""
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=1)
    from app.services.quiz import QUIZ_STALE_SESSION_SEC
    session.question_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=QUIZ_STALE_SESSION_SEC + 60
    )

    assert _is_session_stale(session) is True


def test_is_session_stale_none_started_at_after_questions() -> None:
    """Если question_started_at is None после вопросов — сессия зависшая (между вопросами)."""
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=3)
    session.question_started_at = None

    assert _is_session_stale(session) is True


def test_is_session_stale_none_started_at_fresh() -> None:
    """Если question_started_at is None и question_number=0 — сессия только создана, не зависшая."""
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=0)
    session.question_started_at = None

    assert _is_session_stale(session) is False


def test_is_session_stale_recent_naive_not_stale() -> None:
    """Недавняя naive datetime не считается зависшей."""
    session = QuizSession(chat_id=1, topic_id=1, is_active=True, question_number=1)
    # Naive datetime = «сейчас UTC» (без tzinfo)
    session.question_started_at = datetime.utcnow()  # naive, но свежий

    result = _is_session_stale(session)
    # Должно быть False — только что запущенная сессия не зависшая
    assert result is False
