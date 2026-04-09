"""Почему: гарантируем что одно message_id не модерируется дважды,
и после TTL запись удаляется и модерация снова возможна."""

from __future__ import annotations

import time

import pytest

from app.handlers.moderation import _is_already_moderated, _MODERATED_MSG_IDS


@pytest.fixture(autouse=True)
def clear_moderated_cache():
    """Очищаем словарь перед каждым тестом."""
    _MODERATED_MSG_IDS.clear()
    yield
    _MODERATED_MSG_IDS.clear()


def test_first_call_not_duplicate() -> None:
    """Первый вызов с новым message_id не является дублем."""
    assert _is_already_moderated(12345) is False


def test_second_call_is_duplicate() -> None:
    """Второй вызов с тем же message_id — дубль."""
    _is_already_moderated(99999)
    assert _is_already_moderated(99999) is True


def test_different_ids_not_duplicate() -> None:
    """Разные message_id не влияют друг на друга."""
    _is_already_moderated(111)
    _is_already_moderated(222)
    assert _is_already_moderated(333) is False


def test_ttl_expiry_allows_remoderation(monkeypatch) -> None:
    """После истечения TTL то же message_id можно модерировать снова."""
    # Регистрируем сообщение с задержкой времени в прошлом
    msg_id = 77777
    # Напрямую вставляем запись с устаревшим timestamp
    from app.handlers.moderation import _MODERATED_MSG_IDS_TTL
    _MODERATED_MSG_IDS[msg_id] = time.monotonic() - _MODERATED_MSG_IDS_TTL - 1.0

    # Следующий вызов должен считать запись устаревшей и вернуть False
    result = _is_already_moderated(msg_id)
    assert result is False, "После TTL должно быть разрешено снова модерировать"


def test_ttl_cleanup_removes_expired_entries() -> None:
    """Вызов _is_already_moderated очищает устаревшие записи."""
    from app.handlers.moderation import _MODERATED_MSG_IDS_TTL

    # Добавляем несколько устаревших записей
    for i in range(5):
        _MODERATED_MSG_IDS[i] = time.monotonic() - _MODERATED_MSG_IDS_TTL - 1.0

    assert len(_MODERATED_MSG_IDS) == 5

    # Любой вызов запустит TTL-очистку
    _is_already_moderated(9999)

    # Устаревшие записи должны быть удалены
    assert len(_MODERATED_MSG_IDS) == 1  # только что добавленный 9999


def test_fresh_entry_not_cleaned_by_ttl() -> None:
    """Свежие записи не удаляются TTL-очисткой."""
    _is_already_moderated(10001)
    _is_already_moderated(10002)

    # Оба ID зарегистрированы как свежие
    assert 10001 in _MODERATED_MSG_IDS
    assert 10002 in _MODERATED_MSG_IDS

    # Обращение к третьему ID не должно удалить свежие
    _is_already_moderated(10003)
    assert 10001 in _MODERATED_MSG_IDS
    assert 10002 in _MODERATED_MSG_IDS
