"""Тесты topic-aware контекста ассистента."""
from __future__ import annotations

import pytest


def test_topic_hint_returns_empty_for_none():
    from app.services.ai_module import get_topic_hint
    assert get_topic_hint(None) == ""


def test_topic_hint_returns_empty_for_unknown():
    from app.services.ai_module import get_topic_hint
    assert get_topic_hint(999999) == ""


def test_topic_hint_contains_topic_name_for_known_id():
    """Для любого зарегистрированного topic_id подсказка непустая и содержит метку контекста."""
    from app.services.ai_module import get_topic_hint, _build_topic_context_map
    mapping = _build_topic_context_map()
    if not mapping:
        pytest.skip("Нет настроенных topic_id в конфиге")
    topic_id = next(iter(mapping))
    hint = get_topic_hint(topic_id)
    assert hint.startswith("\n[Контекст топика")
    assert len(hint) > 20


def test_topic_hint_gate_contains_shlabgaum(monkeypatch):
    """Если topic_gate настроен, подсказка содержит слово про шлагбаум."""
    import app.services.ai_module as ai_mod
    # Принудительно добавляем тестовый топик
    ai_mod._TOPIC_CONTEXT_MAP = {12345: ("Шлагбаум", "шлагбаум пропуска Дворецкий")}
    hint = ai_mod.get_topic_hint(12345)
    assert "шлагбаум" in hint.lower()
    # Сбрасываем
    ai_mod._TOPIC_CONTEXT_MAP = {}
