"""Почему: дать жителю простой способ отключить/включить еженедельные DM-нажъмы."""

from __future__ import annotations

import json
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.db import get_session
from app.models import ResidentProfile

logger = logging.getLogger(__name__)
router = Router()


def _load_facts(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


async def _set_nudge_flag(user_id: int, *, opt_out: bool) -> int:
    """Обновляет nudge_opt_out во всех профилях пользователя. Возвращает число
    затронутых профилей (обычно 0 или 1, может быть больше при нескольких чатах).
    """

    touched = 0
    async for session in get_session():
        rows = await session.execute(
            select(ResidentProfile).where(ResidentProfile.user_id == user_id)
        )
        for profile in rows.scalars():
            facts = _load_facts(profile.facts_json)
            if opt_out:
                facts["nudge_opt_out"] = True
            else:
                facts.pop("nudge_opt_out", None)
                # Включение опт-ина одновременно сбрасывает unreachable —
                # житель явно общается с ботом, значит DM работает.
                facts.pop("nudge_unreachable", None)
            profile.facts_json = json.dumps(facts, ensure_ascii=False)
            touched += 1
        await session.commit()
        break
    return touched


@router.message(Command("off_nudges"))
async def off_nudges(message: Message) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    touched = await _set_nudge_flag(message.from_user.id, opt_out=True)
    if touched == 0:
        await message.answer(
            "Готово: я и так ничего вам персонально не присылал. Если что — пишите в группу."
        )
        return
    await message.answer(
        "Готово — больше не буду присылать персональные сообщения. "
        "Если передумаете, напишите /on_nudges."
    )
    logger.info("NUDGE_OPT_OUT: user_id=%s profiles=%d", message.from_user.id, touched)


@router.message(Command("on_nudges"))
async def on_nudges(message: Message) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    touched = await _set_nudge_flag(message.from_user.id, opt_out=False)
    if touched == 0:
        await message.answer(
            "У меня пока нет вашего профиля — напишите что-нибудь в группе, и я начну запоминать."
        )
        return
    await message.answer(
        "Готово — снова смогу присылать вам редкие персональные подсказки."
    )
    logger.info("NUDGE_OPT_IN: user_id=%s profiles=%d", message.from_user.id, touched)
