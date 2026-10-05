import pytest
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
    """График приёма УК «ВЕК» по объявлению от октября 2026."""
    answer = build_resident_answer("Как работает УК?")
    assert answer is not None
    assert "не приёмный день" in answer          # понедельник
    assert "09:00–18:00" in answer               # вт–чт
    assert "09:00–17:00" in answer               # пт
    assert "10:00–14:00" in answer               # сб
    assert "13:00–14:00" in answer               # обед
    assert "085-33-30" in answer                 # вс — заявки через аварийку


def test_old_uk_schedule_is_gone_everywhere() -> None:
    """Старый график (перерыв до 13:48, среда до 19:00, выходная суббота)
    не должен остаться ни в одной записи — иначе бот ответит двумя версиями."""
    from app.services.resident_kb import load_resident_kb

    load_resident_kb.cache_clear()
    for entry in load_resident_kb():
        assert "13:48" not in entry.answer, entry.id
        assert "Ср — 09:00–19:00" not in entry.answer, entry.id
        assert "Сб, Вс — выходные" not in entry.answer, entry.id


@pytest.mark.parametrize(("question", "expected"), [
    ("Работает ли УК в субботу?", "10:00–14:00"),
    ("УК в понедельник принимает?", "не приёмный день"),
    ("Какая почта у УК?", "info@ukvek-sity.ru"),
])
def test_new_uk_questions_find_answers(question: str, expected: str) -> None:
    answer = build_resident_answer(question)
    assert answer is not None and expected in answer


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
    assert len(answer.strip()) > 0


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

