"""Почему: тестируем in-memory circuit breaker OpenRouterProvider.

Состояния:
  CLOSED → после 3 ошибок подряд → OPEN
  OPEN → через _CB_RECOVERY_TIMEOUT_SEC → HALF_OPEN
  HALF_OPEN + успех → CLOSED
  HALF_OPEN + ошибка → OPEN снова
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_module import (
    OpenRouterProvider,
    _CB_FAILURE_THRESHOLD,
    _CB_RECOVERY_TIMEOUT_SEC,
)


@pytest.fixture()
def provider():
    """Создаём провайдер с заглушками для HTTP и настроек."""
    with (
        patch("app.services.ai_module.settings") as mock_settings,
        patch("app.services.ai_module.httpx.AsyncClient"),
    ):
        mock_settings.ai_key = "test-key"
        mock_settings.ai_api_url = "https://example.com"
        mock_settings.ai_timeout_seconds = 10
        mock_settings.ai_model = "anthropic/claude-haiku-4.5"
        mock_settings.ai_retries = 0
        mock_settings.ai_max_tokens = 100
        mock_settings.ai_daily_request_limit = 2000
        mock_settings.ai_daily_token_limit = 400000

        p = OpenRouterProvider()
        p._client = AsyncMock()
        yield p


def test_initial_state_is_closed(provider) -> None:
    """Начальное состояние — CLOSED."""
    assert provider.get_circuit_breaker_state() == "CLOSED"


def test_failures_transition_to_open(provider) -> None:
    """После _CB_FAILURE_THRESHOLD ошибок подряд → OPEN."""
    for _ in range(_CB_FAILURE_THRESHOLD):
        assert provider.get_circuit_breaker_state() == "CLOSED"
        provider._cb_record_failure()

    assert provider.get_circuit_breaker_state() == "OPEN"


def test_open_blocks_request(provider) -> None:
    """В состоянии OPEN _cb_check_before_request возвращает False."""
    # Переводим в OPEN
    for _ in range(_CB_FAILURE_THRESHOLD):
        provider._cb_record_failure()

    assert provider.get_circuit_breaker_state() == "OPEN"
    assert provider._cb_check_before_request() is False


def test_open_to_half_open_after_timeout(provider) -> None:
    """После recovery timeout OPEN переходит в HALF_OPEN."""
    for _ in range(_CB_FAILURE_THRESHOLD):
        provider._cb_record_failure()

    # Имитируем прошедшее время (устанавливаем opened_at в прошлое)
    provider._cb_opened_at = time.monotonic() - _CB_RECOVERY_TIMEOUT_SEC - 1

    # Следующая проверка должна разрешить запрос и перейти в HALF_OPEN
    allowed = provider._cb_check_before_request()
    assert allowed is True
    assert provider.get_circuit_breaker_state() == "HALF_OPEN"


def test_half_open_success_transitions_to_closed(provider) -> None:
    """Успешный запрос в HALF_OPEN → CLOSED."""
    # Вручную устанавливаем HALF_OPEN
    provider._cb_state = "HALF_OPEN"
    provider._cb_failures = _CB_FAILURE_THRESHOLD

    provider._cb_record_success()

    assert provider.get_circuit_breaker_state() == "CLOSED"
    assert provider._cb_failures == 0


def test_half_open_failure_returns_to_open(provider) -> None:
    """Ошибка в HALF_OPEN → OPEN снова."""
    provider._cb_state = "HALF_OPEN"
    provider._cb_failures = _CB_FAILURE_THRESHOLD

    provider._cb_record_failure()

    assert provider.get_circuit_breaker_state() == "OPEN"


def test_success_resets_failures(provider) -> None:
    """Успешный запрос сбрасывает счётчик ошибок."""
    provider._cb_failures = 2  # накопили 2 ошибки, но ещё CLOSED
    provider._cb_record_success()

    assert provider._cb_failures == 0
    assert provider.get_circuit_breaker_state() == "CLOSED"


def test_closed_allows_request(provider) -> None:
    """В состоянии CLOSED запросы всегда разрешены."""
    assert provider.get_circuit_breaker_state() == "CLOSED"
    assert provider._cb_check_before_request() is True


def test_failure_threshold_exactly(provider) -> None:
    """Ровно _CB_FAILURE_THRESHOLD ошибок переводит в OPEN."""
    for i in range(_CB_FAILURE_THRESHOLD - 1):
        provider._cb_record_failure()
        assert provider.get_circuit_breaker_state() == "CLOSED", (
            f"После {i + 1} ошибок должен быть CLOSED"
        )

    provider._cb_record_failure()
    assert provider.get_circuit_breaker_state() == "OPEN"
