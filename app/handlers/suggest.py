"""Почему: жители могут предлагать новые места инфраструктуры напрямую через бота."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.services.sheets import write_suggestion

router = Router()
logger = logging.getLogger(__name__)

_USAGE = (
    "Используй формат:\n"
    "/предложить Название | Категория | Адрес\n\n"
    "Необязательно: | Описание | Телефон | Сайт\n\n"
    "Пример:\n"
    "/предложить Аптека Здоровье | Аптека | ул. Бутово, 5 | Круглосуточно | +7-495-000-00-00"
)


def _parse_suggestion(text: str) -> dict[str, str] | None:
    """Разбирает строку предложения на поля через `|`."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3:
        return None
    name, category, address = parts[0], parts[1], parts[2]
    if not name or not category or not address:
        return None
    return {
        "name": name,
        "category": category,
        "address": address,
        "description": parts[3] if len(parts) > 3 else "",
        "phone": parts[4] if len(parts) > 4 else "",
        "website": parts[5] if len(parts) > 5 else "",
    }


@router.message(Command("предложить"))
async def suggest_place(message: Message) -> None:
    if message.chat.id != settings.forum_chat_id and message.chat.type != "private":
        return

    raw = (message.text or "").removeprefix("/предложить").strip()
    if not raw:
        await message.reply(_USAGE)
        return

    fields = _parse_suggestion(raw)
    if fields is None:
        await message.reply(f"Не удалось разобрать предложение.\n\n{_USAGE}")
        return

    user = message.from_user
    user_name = user.full_name if user else "Неизвестный"
    user_id = user.id if user else 0

    try:
        await write_suggestion(
            name=fields["name"],
            category=fields["category"],
            address=fields["address"],
            description=fields["description"],
            phone=fields["phone"],
            website=fields["website"],
            user_name=user_name,
            user_id=user_id,
        )
        await message.reply(
            f"Спасибо! Предложение «{fields['name']}» отправлено на рассмотрение.\n"
            "Если оно будет одобрено — появится в базе мест ЖК."
        )
        logger.info(
            "SUGGEST: user=%s (id=%s) предложил '%s' (%s)",
            user_name, user_id, fields["name"], fields["category"],
        )
    except Exception:
        logger.exception("SUGGEST: ошибка записи предложения в Sheets")
        await message.reply("Не удалось сохранить предложение. Попробуй позже.")
