"""Регрессии аудита-1: порог RAG, чистый вопрос, шифрование бэкапа."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


def _rag_msg(text: str, *, is_admin: bool = False):
    return SimpleNamespace(
        message_text=text,
        rag_canonical_text=None,
        is_admin=is_admin,
        created_at=datetime.now(timezone.utc),
        rag_category=None,
        rag_semantic_key=None,
    )


def test_rag_drops_irrelevant_documents() -> None:
    """Главный фикс аудита: нерелевантные записи НЕ попадают в опору.

    Раньше на любой вопрос возвращался топ-8 (даже про шлагбаум на вопрос о
    катке) → гейт от галлюцинаций никогда не срабатывал.
    """
    from app.services.rag import rank_rag_messages

    messages = [
        _rag_msg("Шлагбаум открывается через приложение УК", is_admin=True),
        _rag_msg("Пропуск на автомобиль оформляется у диспетчера"),
        _rag_msg("Показания счётчиков подают до 25 числа"),
    ]
    # Вопрос совсем не про это — опора должна быть ПУСТОЙ.
    ranked = rank_rag_messages(messages, query="где ближайший каток залить лёд")
    assert ranked == []


def test_rag_keeps_relevant_documents() -> None:
    from app.services.rag import rank_rag_messages

    messages = [
        _rag_msg("Шлагбаум открывается через приложение УК", is_admin=True),
        _rag_msg("Показания счётчиков подают до 25 числа"),
    ]
    ranked = rank_rag_messages(messages, query="как открыть шлагбаум")
    assert len(ranked) >= 1
    assert "Шлагбаум" in ranked[0].message_text


def test_clean_question_survives_long_context() -> None:
    """Вопрос жителя в конце длинного промпта не должен отрезаться."""
    from app.services.ai_module import _strip_prompt_scaffolding

    context_lines = "\n".join(f"- сосед {i}: бла-бла-бла что-то про погоду" for i in range(30))
    full_prompt = (
        "[Недавние сообщения в этой теме — контекст беседы]\n"
        f"{context_lines}\n\n"
        "[Реплика, на которую отвечаешь]\n"
        "Где ближайшая аптека?"
    )
    assert len(full_prompt) > 1000  # раньше [:1000] отрезал вопрос целиком
    clean = _strip_prompt_scaffolding(full_prompt)
    assert clean == "Где ближайшая аптека?"


def test_backup_encrypt_roundtrip(tmp_path) -> None:
    """Шифрование бэкапа: файл расшифровывается тем же ключом."""
    from cryptography.fernet import Fernet

    from app.services.backup import _encrypt_file

    key = Fernet.generate_key().decode()
    src = tmp_path / "bot.db"
    src.write_bytes(b"sqlite fake data \x00\x01")
    dst = tmp_path / "bot.db.enc"
    _encrypt_file(src, dst, key)

    assert dst.read_bytes() != src.read_bytes()  # реально зашифровано
    decrypted = Fernet(key.encode()).decrypt(dst.read_bytes())
    assert decrypted == src.read_bytes()
