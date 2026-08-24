"""Тесты модулей, остававшихся без покрытия (аудит: 10 модулей вслепую).

Проверяем поведение, от которого зависят реальные сценарии: профиль жителя,
настроение чата, решение о веб-поиске, безопасные вызовы Telegram, роутинг
моделей и разбор /предложить.
"""

from __future__ import annotations

import asyncio

import pytest


# --- resident_profile: разбор фактов из ответа модели ---

def test_parse_extracted_facts_keeps_known_fields_only() -> None:
    from app.services.resident_profile import PROFILE_FIELDS, parse_extracted_facts

    known = next(iter(PROFILE_FIELDS))
    parsed = parse_extracted_facts(
        '{"%s": "значение", "секретное_поле": "мусор"}' % known
    )
    assert parsed == {known: "значение"}


@pytest.mark.parametrize("raw", ["", "не json", "null", "[1, 2]", "{}"])
def test_parse_extracted_facts_survives_garbage(raw: str) -> None:
    """Модель иногда отвечает не-JSON — профиль не должен ломаться."""
    from app.services.resident_profile import parse_extracted_facts

    assert parse_extracted_facts(raw) == {}


def test_format_profile_for_prompt_empty_is_empty() -> None:
    from app.services.resident_profile import format_profile_for_prompt

    assert format_profile_for_prompt({}) == ""


# --- mood: настроение чата по буферу sentiment'ов ---

def test_mood_needs_minimum_messages() -> None:
    """На двух сообщениях настроение не считаем — статистика ни о чём."""
    from app.services.mood import ChatMood, get_mood, record_sentiment
    from app.services import mood as mood_module

    mood_module._MOOD_BUFFER.clear()
    record_sentiment(1, 1, "negative")
    record_sentiment(1, 1, "negative")
    assert get_mood(1, 1).mood == ChatMood.NEUTRAL


def test_mood_turns_negative_when_chat_heats_up() -> None:
    from app.services.mood import ChatMood, get_mood, record_sentiment
    from app.services import mood as mood_module

    mood_module._MOOD_BUFFER.clear()
    for _ in range(10):
        record_sentiment(2, None, "negative")
    snapshot = get_mood(2, None)
    assert snapshot.mood in (ChatMood.TENSE, ChatMood.ANGRY)
    assert snapshot.negative_pct > 0.5
    assert snapshot.total_messages == 10


def test_mood_ignores_unknown_sentiment() -> None:
    from app.services.mood import get_mood, record_sentiment
    from app.services import mood as mood_module

    mood_module._MOOD_BUFFER.clear()
    for _ in range(5):
        record_sentiment(3, None, "ЯРОСТЬ")  # не из словаря
    assert get_mood(3, None).total_messages == 0


def test_mood_style_hint_exists_for_every_mood() -> None:
    from app.services.mood import ChatMood, get_mood_style_hint

    for mood in ChatMood:
        assert isinstance(get_mood_style_hint(mood), str)


# --- web_search: когда лезем в интернет ---

@pytest.mark.parametrize("prompt", [
    "погугли когда открывается новый парк",
    "какие новости про метро",
    "посмотри https://example.com что пишут",
])
def test_web_search_triggers_on_external_questions(prompt: str) -> None:
    from app.services.web_search import should_search_web

    assert should_search_web(prompt) is True


@pytest.mark.parametrize("prompt", [
    "как заказать пропуск на шлагбаум",
    "телефон ук",
])
def test_web_search_skipped_for_internal_questions(prompt: str) -> None:
    """Вопросы про ЖК отвечаются из базы, интернет тут только навредит."""
    from app.services.web_search import should_search_web

    assert should_search_web(prompt) is False


def test_web_search_misses_inflected_triggers() -> None:
    """Известный пробел: триггеры сравниваются подстрокой, поэтому «в новостях»
    (косвенный падеж) веб-поиск не запускает, а «новости» — запускает.

    Тест фиксирует текущее поведение: если начнём приводить слова к леммам,
    он упадёт и напомнит обновить ожидания.
    """
    from app.services.web_search import should_search_web

    assert should_search_web("какие новости") is True
    assert should_search_web("что там в новостях") is False


def test_web_search_disabled_by_setting(monkeypatch) -> None:
    from app.services import web_search

    monkeypatch.setattr(web_search.settings, "ai_feature_web_search", False)
    assert web_search.should_search_web("погугли новости") is False


def test_format_search_context_empty_and_filled() -> None:
    from app.services.web_search import format_search_context

    assert format_search_context([]) == ""
    text = format_search_context([
        {"title": "Аптека", "snippet": "работает круглосуточно", "url": "https://a.ru"},
    ])
    assert "Аптека" in text and "круглосуточно" in text


# --- safe_telegram: сбои Telegram не роняют вызывающий код ---

def test_safe_call_returns_value_and_swallows_errors(caplog) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from app.utils.safe_telegram import safe_call

    async def ok():
        return "готово"

    async def boom():
        raise TelegramBadRequest(method=None, message="message is not modified")

    assert asyncio.run(safe_call(ok(), log_ctx="тест")) == "готово"
    assert asyncio.run(safe_call(boom(), log_ctx="тест")) is None


# --- ai_router: маршрутизация задач по моделям ---

def test_router_returns_model_for_known_and_unknown_task() -> None:
    from app.services.ai_router import get_model_for_task

    assert get_model_for_task("reply")
    # Неизвестная задача не должна ронять вызов — нужен рабочий дефолт.
    assert get_model_for_task("что-то-новое")


def test_router_limits_are_sane() -> None:
    from app.services.ai_router import get_max_tokens_for_task, get_temperature_for_task

    assert 0.0 <= get_temperature_for_task("reply") <= 1.5
    assert get_max_tokens_for_task("reply") > 0


# --- suggest: разбор «/предложить Название | Категория | Адрес» ---

def test_suggestion_parsed_with_optional_fields() -> None:
    from app.handlers.suggest import _parse_suggestion

    parsed = _parse_suggestion(
        "Пекарня У дома | еда | ул. Троицкая, 5 | вкусный хлеб | +7 495 000-00-00 | https://a.ru"
    )
    assert parsed == {
        "name": "Пекарня У дома",
        "category": "еда",
        "address": "ул. Троицкая, 5",
        "description": "вкусный хлеб",
        "phone": "+7 495 000-00-00",
        "website": "https://a.ru",
    }


@pytest.mark.parametrize("text", [
    "Только название",
    "Название | категория",       # нет адреса
    "  |  |  ",                   # пустые поля
])
def test_incomplete_suggestion_rejected(text: str) -> None:
    from app.handlers.suggest import _parse_suggestion

    assert _parse_suggestion(text) is None


# --- admin_corrections: распознавание поправки от админа ---

@pytest.mark.parametrize("text", [
    "Нет, это неправильно — телефон поменяли",
    "На самом деле аптека давно закрыли",
    "Ты ошибаешься, адрес другой",
    "Правильный номер будет +7 495 000-00-00",
])
def test_admin_correction_detected(text: str) -> None:
    from app.services.admin_corrections import is_admin_correction

    assert is_admin_correction(text) is True


@pytest.mark.parametrize("text", [
    "спасибо",                      # короткое — не поправка
    "Да, всё верно, так и есть",
    "А где находится аптека?",
])
def test_ordinary_messages_are_not_corrections(text: str) -> None:
    """Ложная поправка отравила бы базу знаний, поэтому важнее не перестараться."""
    from app.services.admin_corrections import is_admin_correction

    assert is_admin_correction(text) is False


# --- ai_schemas: дефолты безопасны (модель может вернуть пустой JSON) ---

def test_moderation_schema_defaults_are_permissive() -> None:
    """Пустой ответ модели не должен превращаться в наказание жителя."""
    from app.services.ai_schemas import ModerationResult, SpamResult

    m = ModerationResult()
    assert m.is_violation is False and m.recommended_action == "log_only"
    s = SpamResult()
    assert s.is_spam is False and s.recommended_action == "log_only"


def test_schemas_ignore_unknown_fields() -> None:
    from app.services.ai_schemas import ModerationResult

    result = ModerationResult(**{"is_violation": True, "confidence": 0.9})
    assert result.is_violation is True and result.confidence == 0.9


# --- admin_help: меню и справка собираются ---

def test_admin_menu_and_help_are_built() -> None:
    from app.utils.admin_help import ADMIN_CATEGORIES, ADMIN_HELP, admin_menu_keyboard

    assert ADMIN_CATEGORIES and ADMIN_HELP.strip()
    keyboard = admin_menu_keyboard()
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    # У каждой категории — своя кнопка с корректным callback_data.
    assert len(buttons) >= len(ADMIN_CATEGORIES)
    assert all(b.callback_data.startswith("adm:") for b in buttons)
