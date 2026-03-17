"""Почему: проверяем ключевую логику выбора топиков для команды /text без Telegram API."""

from app.handlers.text_publish import _topic_keyboard, _topic_options


def test_topic_options_include_main_chat(monkeypatch) -> None:
    monkeypatch.setattr("app.handlers.text_publish.settings.topic_rules", None)
    monkeypatch.setattr("app.handlers.text_publish.settings.topic_gate", 321)

    options = _topic_options()

    assert options[0].title == "Главный чат"
    assert options[0].topic_id is None
    assert any(item.title == "Шлагбаум" and item.topic_id == 321 for item in options)


def test_topic_keyboard_contains_cancel_and_main_topic() -> None:
    keyboard = _topic_keyboard(user_id=77)
    all_buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert any(btn.callback_data == "txt:topic:77:main" for btn in all_buttons)
    assert any(btn.callback_data == "txt:cancel:77" for btn in all_buttons)
