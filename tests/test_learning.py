"""Тесты сервиса автокоррекции."""
from __future__ import annotations


def test_is_likely_correction_detects_pattern():
    from app.services.learning import is_likely_correction
    assert is_likely_correction(
        "Нет, аптека на Сухановской, а не на Лесной", "Аптека на Лесной"
    )


def test_is_likely_correction_nah_na_samom_dele():
    from app.services.learning import is_likely_correction
    assert is_likely_correction("А на самом деле там магазин Пятёрочка", "Там Дикси")


def test_is_likely_correction_uzhe_ne_rabotaet():
    from app.services.learning import is_likely_correction
    assert is_likely_correction("Этот номер уже не работает, сменили.", "Телефон 495-123-4567")


def test_is_likely_correction_neправильно():
    from app.services.learning import is_likely_correction
    assert is_likely_correction("Это неправильно, режим работы с 9 до 18.", "Работают с 8 до 20")


def test_is_likely_correction_ignores_short():
    from app.services.learning import is_likely_correction
    assert not is_likely_correction("Нет", "Аптека на Лесной")


def test_is_likely_correction_ignores_thanks():
    from app.services.learning import is_likely_correction
    assert not is_likely_correction("Спасибо большое за информацию!", "Аптека на Лесной")


def test_is_likely_correction_ignores_agreement():
    from app.services.learning import is_likely_correction
    assert not is_likely_correction("Да, всё верно, так и есть!", "Аптека на Лесной")


def test_pending_correction_queue_roundtrip():
    """Коррекция кладётся в очередь модерации и извлекается один раз."""
    from app.services.learning import pop_pending_correction, store_pending_correction

    payload = {"chat_id": 1, "user_id": 2, "corrected_text": "[Коррекция от жителя] тест", "fact": "тест"}
    uid = store_pending_correction(payload)
    assert pop_pending_correction(uid) == payload
    # Повторное извлечение — None (одноразовость)
    assert pop_pending_correction(uid) is None


def test_pending_correction_queue_caps_size():
    """Очередь не растёт бесконечно: старые записи вытесняются."""
    from app.services import learning

    for i in range(learning._PENDING_CORRECTIONS_MAX + 10):
        learning.store_pending_correction({"n": i})
    assert len(learning._PENDING_CORRECTIONS) <= learning._PENDING_CORRECTIONS_MAX
    learning._PENDING_CORRECTIONS.clear()
