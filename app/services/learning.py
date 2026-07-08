"""Почему: бот обучается на коррекциях от жителей — если бота поправили, он запоминает."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Паттерны коррекции: пользователь поправляет бота
_CORRECTION_PATTERNS = [
    # Прямое отрицание
    re.compile(r"(?:нет|не)\s*,?\s*(?:это|там|тут|на самом деле|правильно)", re.I),
    re.compile(r"(?:не\s+так|неправильно|ошибка|ошибаешься|ты\s+не\s+прав)", re.I),
    re.compile(r"(?:а\s+на\s+самом\s+деле|на\s+самом\s+деле|вообще-то)", re.I),
    # Указание на устаревшие данные
    re.compile(r"(?:уже\s+не\s+работает|уже\s+закрыл|уже\s+переехал|уже\s+не\s+так)", re.I),
    re.compile(r"(?:номер\s+(?:сменил|изменил|теперь)|адрес\s+(?:другой|изменился))", re.I),
    re.compile(r",\s*а\s+не\s+(?:на|в|по|у)\b", re.I),  # "на Сухановской, а не на Лесной"
    # Указание на изменение данных (телефон, адрес, расписание)
    re.compile(r"(?:телефон|номер)\s+(?:поменяли|поменялся|сменили|другой|новый|теперь)", re.I),
    re.compile(r"(?:адрес|расположение)\s+(?:поменяли|поменялся|сменили|другой|теперь)", re.I),
    re.compile(r"(?:расписание|график|время)\s+(?:поменяли|поменялось|изменили|другое|теперь)", re.I),
    # Числовые коррекции ("не 8-800, а 8-495")
    re.compile(r"не\s+\d[\d\-\s]+,?\s*а\s+\d", re.I),
    # Мягкие коррекции
    re.compile(r"(?:там\s+уже\s+другой|это\s+устарело|это\s+старая\s+информация)", re.I),
    re.compile(r"(?:давно\s+закрыли|давно\s+переехали|больше\s+не\s+работает)", re.I),
    re.compile(r"(?:сейчас\s+(?:уже|там)\s+(?:другой|другая|другое|нет))", re.I),
    # Прямое исправление
    re.compile(r"(?:правильн\w+\s+(?:будет|ответ|номер|адрес|название))", re.I),
    re.compile(r"(?:точн\w+\s+(?:адрес|номер|название|телефон))", re.I),
]


def is_likely_correction(user_text: str, bot_reply: str) -> bool:
    """Быстрая проверка: похоже ли сообщение на коррекцию бота."""
    if len(user_text) < 10:
        return False
    return any(p.search(user_text) for p in _CORRECTION_PATTERNS)


async def _check_duplicate_correction(
    session: "AsyncSession",
    chat_id: int,
    semantic_key: str,
) -> bool:
    """Проверяет, есть ли уже активная коррекция с таким же смысловым ключом."""
    from datetime import datetime, timezone
    from sqlalchemy import and_, select
    from app.models import RagMessage

    now = datetime.now(timezone.utc)
    result = await session.scalar(
        select(RagMessage.id).where(
            and_(
                RagMessage.chat_id == chat_id,
                RagMessage.rag_semantic_key == semantic_key,
                RagMessage.message_text.like("[Коррекция от жителя]%"),
                (RagMessage.expires_at.is_(None)) | (RagMessage.expires_at > now),
            )
        ).limit(1)
    )
    return result is not None


# Очередь коррекций, ожидающих подтверждения админом: uid -> payload.
# Коррекции жителей НЕ пишутся в RAG напрямую (вектор отравления базы) —
# только после нажатия «Принять» админом (callback corr:ok:/corr:no: в admin.py).
_PENDING_CORRECTIONS: dict[str, dict] = {}
_PENDING_CORRECTIONS_MAX = 50


def store_pending_correction(payload: dict) -> str:
    """Сохраняет коррекцию в очередь модерации, возвращает uid для callback-кнопок."""
    import uuid
    if len(_PENDING_CORRECTIONS) >= _PENDING_CORRECTIONS_MAX:
        oldest_key = next(iter(_PENDING_CORRECTIONS))
        _PENDING_CORRECTIONS.pop(oldest_key, None)
    uid = uuid.uuid4().hex[:12]
    _PENDING_CORRECTIONS[uid] = payload
    return uid


def pop_pending_correction(uid: str) -> dict | None:
    """Извлекает коррекцию из очереди (одноразово)."""
    return _PENDING_CORRECTIONS.pop(uid, None)


async def _send_correction_for_review(
    *,
    chat_id: int,
    user_id: int,
    fact: str,
    corrected_text: str,
    bot: object | None = None,
) -> None:
    """Отправляет коррекцию админам на подтверждение с inline-кнопками."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.config import settings
        admin_chat = settings.admin_log_chat_id
        if not (admin_chat and bot and hasattr(bot, "send_message")):
            return
        uid = store_pending_correction({
            "chat_id": chat_id,
            "user_id": user_id,
            "corrected_text": corrected_text,
            "fact": fact,
        })
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"corr:ok:{uid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"corr:no:{uid}"),
        ]])
        text = (
            f"📝 Коррекция от жителя (user_id={user_id}) — требует проверки:\n\n"
            f"{fact[:500]}\n\n"
            "«Принять» → запись в RAG на 180 дней. «Отклонить» → игнорировать."
        )
        await bot.send_message(admin_chat, text, reply_markup=keyboard)
    except Exception:
        # Уведомление не критично — логируем и идём дальше
        logger.warning("Не удалось отправить коррекцию на модерацию.")


async def detect_and_apply_correction(
    session: "AsyncSession",
    *,
    chat_id: int,
    user_id: int,
    user_text: str,
    bot_reply: str,
    bot: object | None = None,
) -> bool:
    """Определяет коррекцию и ставит её в очередь модерации админам.

    Возвращает True, если коррекция распознана и отправлена на проверку.
    В RAG запись попадает только после подтверждения админом (защита от
    отравления базы знаний произвольными «поправками»).
    """
    if not is_likely_correction(user_text, bot_reply):
        return False

    # Пробуем извлечь факт через AI
    from app.services.ai_module import get_ai_client
    ai_client = get_ai_client()
    try:
        prompt = (
            f"Бот ответил: {bot_reply[:300]}\n"
            f"Пользователь поправил: {user_text[:300]}\n"
            "Извлеки правильную информацию. Если это действительно коррекция — "
            'верни JSON: {"is_correction": true, "fact": "правильная информация"}\n'
            'Если это не коррекция — верни: {"is_correction": false}'
        )
        raw = await ai_client.extract_user_facts(prompt, chat_id=chat_id)
        data = json.loads(raw)
        if not data.get("is_correction"):
            return False
        fact = str(data.get("fact", ""))
        if not fact or len(fact) < 5:
            return False
    except Exception:
        logger.warning("Не удалось извлечь коррекцию через AI, пропуск.")
        return False

    # Дедупликация: проверяем, нет ли уже такой коррекции в RAG
    from app.services.rag import build_semantic_key, classify_rag_message
    corrected_text = f"[Коррекция от жителя] {fact}"
    category = classify_rag_message(corrected_text)
    semantic_key = build_semantic_key(corrected_text, category)

    if await _check_duplicate_correction(session, chat_id, semantic_key):
        logger.info("LEARNING: дублирующая коррекция пропущена, semantic_key=%s", semantic_key)
        return False

    # НЕ пишем в RAG сразу — отправляем админам на подтверждение.
    await _send_correction_for_review(
        chat_id=chat_id,
        user_id=user_id,
        fact=fact,
        corrected_text=corrected_text,
        bot=bot,
    )
    logger.info("LEARNING: коррекция от user_id=%s отправлена на модерацию: %s", user_id, fact[:100])
    return True
