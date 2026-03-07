from app.handlers.help import _extract_ai_prompt
from app.services.ai_module import build_local_assistant_reply
from app.services.resident_kb import build_resident_answer


class _DummyMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.caption = None
        self.entities = None
        self.caption_entities = None


def test_resident_answer_uk_contacts() -> None:
    answer = build_resident_answer("Где УК и как связаться?")
    assert answer is not None
    assert "УК «ВЕК»" in answer
    assert "+7 (495) 401-60-06" in answer


def test_resident_answer_uk_schedule() -> None:
    answer = build_resident_answer("Как работает УК?")
    assert answer is not None
    assert "09:00–18:00" in answer
    assert "13:00–13:48" in answer


def test_resident_answer_gate() -> None:
    answer = build_resident_answer("Как сделать пропуск в шлагбаум?")
    assert answer is not None
    assert "Дворецкий" in answer
    assert "Пропуска" in answer


def test_resident_answer_empty_guest_pass() -> None:
    answer = build_resident_answer("Если номер гостя неизвестен, что делать?")
    assert answer is not None
    assert "пустой гостевой пропуск" in answer.lower()


def test_resident_answer_lift() -> None:
    answer = build_resident_answer("Куда звонить если лифт не работает?")
    assert answer is not None
    assert "Лифтек" in answer


def test_resident_answer_emergency() -> None:
    answer = build_resident_answer("Куда звонить если заливает квартиру?")
    assert answer is not None
    assert "085-33-30" in answer


def test_resident_answer_internet() -> None:
    answer = build_resident_answer("Какой у нас интернет-провайдер?")
    assert answer is not None
    assert "ГРАНЛАЙН" in answer


def test_resident_answer_video() -> None:
    answer = build_resident_answer("Где взять доступ к камерам?")
    assert answer is not None
    assert "Крепость24.рф" in answer


def test_resident_answer_electricity() -> None:
    answer = build_resident_answer("Куда обращаться по электричеству?")
    assert answer is not None
    assert "Мосэнергосбыт" in answer


def test_fallback_when_unknown_question() -> None:
    answer = build_local_assistant_reply("Где у нас телепорт на Марс?")
    assert "нет" in answer.lower() or "пусто" in answer.lower() or "уточнить" in answer.lower()


def test_mention_normalization_for_username() -> None:
    prompt = _extract_ai_prompt(_DummyMessage("@resident_bot где аварийка?"))
    assert prompt == "где аварийка?"


def test_short_context_followup_for_water_dates() -> None:
    answer = build_resident_answer(
        "А по воде когда?",
        context=["user: Как передать показания?", "assistant: Через МособлЕИРЦ."],
    )
    assert answer is not None
    assert "10 по 19" in answer


def test_local_assistant_reply_uses_context_followup() -> None:
    answer = build_local_assistant_reply(
        "А по воде когда?",
        context=["user: Как передать показания?", "assistant: Передача через МособлЕИРЦ."],
    )
    assert "10 по 19" in answer

