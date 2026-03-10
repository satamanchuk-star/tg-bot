"""Тесты нормализации данных."""

from infra_catalog.core.normalizers import (
    normalize_phone, normalize_text, make_dedup_key,
    normalize_website,
)


def test_normalize_phone_8_prefix():
    result = normalize_phone("8 (495) 541-11-22")
    assert "+7" in result
    assert "5411122" in result.replace(" ", "").replace("-", "")


def test_normalize_phone_multiple():
    result = normalize_phone("+74951112233; 84951112244")
    assert ";" in result


def test_normalize_phone_empty():
    assert normalize_phone("") == ""
    assert normalize_phone("   ") == ""


def test_normalize_text_whitespace():
    assert normalize_text("  hello   world  ") == "hello world"


def test_normalize_text_invisible():
    # \u200b = zero-width space
    assert normalize_text("hello\u200bworld") == "helloworld"


def test_normalize_website_no_scheme():
    assert normalize_website("example.com") == "https://example.com"


def test_normalize_website_with_scheme():
    assert normalize_website("https://example.com") == "https://example.com"


def test_normalize_website_empty():
    assert normalize_website("") == ""


def test_make_dedup_key_normalizes():
    key1 = make_dedup_key("Поликлиника №2", "г. Видное, ул. Заводская, д. 17")
    key2 = make_dedup_key("Поликлиника № 2", "Видное, улица Заводская, дом 17")
    assert key1 == key2


def test_make_dedup_key_strips_brackets():
    key = make_dedup_key("Пункт выдачи (Wildberries)", "адрес")
    assert "wildberries" not in key
