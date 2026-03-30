"""Команды для жителей: поиск услуг и просмотр каталога."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.db import get_session
from app.services.resident_services import (
    CATEGORY_LABELS,
    list_services_by_category,
    search_services,
)

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("услуги", "uslugi"))
async def services_catalog_command(message: Message) -> None:
    """Показывает каталог услуг от жителей по категории или выводит все категории."""
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    async for session in get_session():
        if query:
            services = await search_services(
                session, settings.forum_chat_id, query, top_k=10
            )
            if not services:
                await message.reply(
                    f"Услуги по запросу «{query}» не найдены.\n"
                    "Попробуйте другое слово или /услуги без аргументов для просмотра всех категорий."
                )
                return
            lines = [f"Результаты поиска по «{query}»:\n"]
            for svc in services:
                cat = CATEGORY_LABELS.get(svc.category, svc.category)
                lines.append(f"{cat} — {svc.description[:150]}")
                if svc.provider_name:
                    lines.append(f"   {svc.provider_name}")
            await message.reply("\n".join(lines))
        else:
            all_services = await list_services_by_category(
                session, settings.forum_chat_id, limit=200
            )
            if not all_services:
                await message.reply(
                    "Каталог услуг пока пуст.\n"
                    "Напишите о своей услуге в топике «Услуги от жителей»."
                )
                return

            by_cat: dict[str, list] = defaultdict(list)
            for svc in all_services:
                by_cat[svc.category].append(svc)

            lines = ["Каталог услуг жителей ЖК:\n"]
            for cat_key, svcs in sorted(by_cat.items(), key=lambda x: -len(x[1])):
                label = CATEGORY_LABELS.get(cat_key, cat_key)
                lines.append(f"{label} — {len(svcs)} услуг(а)")

            lines.append(
                "\nДля поиска: /услуги <запрос>\n"
                "Например: /услуги маникюр или /услуги ремонт"
            )
            await message.reply("\n".join(lines))
