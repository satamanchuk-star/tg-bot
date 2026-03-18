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
