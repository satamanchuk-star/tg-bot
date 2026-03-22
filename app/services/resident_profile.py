"""Почему: персональный контекст жителей — бот запоминает факты из диалогов."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ResidentProfile

logger = logging.getLogger(__name__)

# Промпт для извлечения фактов из диалога
EXTRACT_FACTS_PROMPT = (
    "Извлеки факты о пользователе из диалога. Верни только JSON (без пояснений):\n"
    '{"name":null,"building":null,"floor":null,"apartment":null,'
    '"pets":null,"interests":[],"family":null,"car":null,"notes":null}\n'
    "Заполни только те поля, которые ЯВНО упомянуты в тексте. "
    "Если факт не упомянут — оставь null или пустой массив. "
    "Не выдумывай, не додумывай. Краткие значения (1-3 слова на поле)."
)

# Поля, которые бот хранит
PROFILE_FIELDS = ("name", "building", "floor", "apartment", "pets", "interests", "family", "car", "notes")


async def get_profile(session: AsyncSession, user_id: int, chat_id: int) -> dict:
    """Загружает профиль жителя из БД."""
    row = await session.get(ResidentProfile, {"user_id": user_id, "chat_id": chat_id})
    if row is None:
        return {}
    try:
        return json.loads(row.facts_json)
    except (json.JSONDecodeError, TypeError):
        return {}


async def update_profile(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    new_facts: dict,
    display_name: str | None = None,
) -> dict:
    """Мерджит новые факты с существующим профилем. Не затирает старые данные."""
    row = await session.get(ResidentProfile, {"user_id": user_id, "chat_id": chat_id})
    if row is None:
        row = ResidentProfile(user_id=user_id, chat_id=chat_id, facts_json="{}")
        session.add(row)
    try:
        existing = json.loads(row.facts_json)
    except (json.JSONDecodeError, TypeError):
        existing = {}

    # Мерджим: новые не-пустые значения перезаписывают старые
    for key in PROFILE_FIELDS:
        new_val = new_facts.get(key)
        if new_val is None:
            continue
        if isinstance(new_val, list) and not new_val:
            continue
        if isinstance(new_val, str) and not new_val.strip():
            continue
        # Для списков — добавляем уникальные элементы
        if isinstance(new_val, list) and isinstance(existing.get(key), list):
            merged = list(dict.fromkeys(existing[key] + new_val))  # сохраняем порядок
            existing[key] = merged[:10]  # лимит
        else:
            existing[key] = new_val

    row.facts_json = json.dumps(existing, ensure_ascii=False)
    if display_name:
        row.display_name = display_name
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return existing


async def delete_profile(session: AsyncSession, user_id: int, chat_id: int) -> bool:
    """Удаляет профиль жителя (право на забвение)."""
    result = await session.execute(
        delete(ResidentProfile).where(
            ResidentProfile.user_id == user_id,
            ResidentProfile.chat_id == chat_id,
        )
    )
    await session.commit()
    return (result.rowcount or 0) > 0


def format_profile_for_prompt(profile: dict) -> str:
    """Форматирует профиль в строку для системного промпта ассистента."""
    if not profile:
        return ""
    parts = []
    labels = {
        "name": "Имя",
        "building": "Корпус",
        "floor": "Этаж",
        "apartment": "Квартира",
        "pets": "Питомцы",
        "interests": "Интересы",
        "family": "Семья",
        "car": "Машина",
        "notes": "Заметки",
    }
    for key, label in labels.items():
        val = profile.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            if val:
                parts.append(f"{label}: {', '.join(str(v) for v in val)}")
        elif isinstance(val, str) and val.strip():
            parts.append(f"{label}: {val}")
    if not parts:
        return ""
    return "Известные факты о собеседнике:\n" + "\n".join(parts)


def format_profile_for_user(profile: dict) -> str:
    """Форматирует профиль для показа пользователю по /what_you_know."""
    if not profile:
        return "Я пока ничего о тебе не запомнил. Но если пообщаемся — запомню!"
    parts = []
    labels = {
        "name": "Имя",
        "building": "Корпус",
        "floor": "Этаж",
        "apartment": "Квартира",
        "pets": "Питомцы",
        "interests": "Интересы",
        "family": "Семья",
        "car": "Машина",
        "notes": "Другое",
    }
    for key, label in labels.items():
        val = profile.get(key)
        if val is None:
            continue
        if isinstance(val, list) and val:
            parts.append(f"• {label}: {', '.join(str(v) for v in val)}")
        elif isinstance(val, str) and val.strip():
            parts.append(f"• {label}: {val}")
    if not parts:
        return "Я пока ничего о тебе не запомнил. Но если пообщаемся — запомню!"
    return "Вот что я знаю о тебе:\n" + "\n".join(parts) + "\n\nХочешь забыть? Напиши /forget_me"


def parse_extracted_facts(raw_json: str) -> dict:
    """Парсит JSON из ответа AI с извлечёнными фактами."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key in PROFILE_FIELDS:
        val = data.get(key)
        if val is not None:
            result[key] = val
    return result
