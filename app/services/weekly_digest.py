"""Почему: еженедельный дайджест даёт жителям обзор активности форума за неделю."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import Bot

from app.config import settings
from app.db import get_session
from app.models import TopicStat
from app.services.ai_module import get_ai_client
from app.services.daily_messages import fetch_weather, fetch_holidays

logger = logging.getLogger(__name__)

_DIGEST_SYSTEM_PROMPT = (
    "Ты — дружелюбный бот жилого комплекса «Живописный» (Бутово).\n"
    "Напиши краткий еженедельный дайджест для жителей (3-5 предложений).\n"
    "Контекст: итоги недели в чате ЖК. Ниже — статистика активности по разделам.\n"
    "Отметь самые активные темы недели, можно с лёгким юмором.\n"
    "Если есть погода — упомяни как прошла неделя по погоде.\n"
    "Пожелай хороших выходных. Разговорный стиль, тепло и живо.\n"
    "1-2 эмодзи. НЕ пиши длинных текстов."
)

# Топики, которые не включаем в статистику дайджеста
_SKIP_TOPIC_NAMES = {"Правила", "Важное"}


def _build_topic_name_map() -> dict[int, str]:
    topic_data = [
        (settings.topic_gate, "Шлагбаум"),
        (settings.topic_repair, "Ремонт"),
        (settings.topic_complaints, "Жалобы"),
        (settings.topic_pets, "Питомцы"),
        (settings.topic_parents, "Мамы и папы"),
        (settings.topic_realty, "Недвижимость"),
        (settings.topic_services, "Услуги"),
        (settings.topic_uk, "УК"),
        (settings.topic_smoke, "Курилка"),
        (settings.topic_market, "Барахолка"),
        (settings.topic_neighbors, "Соседи"),
        (settings.topic_games, "Игры"),
        (settings.topic_rules, "Правила"),
        (settings.topic_important, "Важное"),
    ]
    mapping: dict[int, str] = {}
    for attr_name in ("topic_buildings_41_42", "topic_building_2", "topic_building_3", "topic_duplex"):
        tid = getattr(settings, attr_name, None)
        if tid is not None:
            label = attr_name.replace("topic_", "").replace("_", " ").title()
            mapping[tid] = label
    for topic_id, name in topic_data:
        if topic_id is not None:
            mapping[topic_id] = name
    return mapping


async def _get_weekly_stats(session: AsyncSession) -> list[tuple[str, int]]:
    """Суммирует сообщения по топикам за последние 7 дней."""
    today = date.today()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(7)]

    rows = (
        await session.execute(
            select(TopicStat.topic_id, func.sum(TopicStat.messages_count).label("total"))
            .where(
                TopicStat.chat_id == settings.forum_chat_id,
                TopicStat.date_key.in_(dates),
            )
            .group_by(TopicStat.topic_id)
            .order_by(func.sum(TopicStat.messages_count).desc())
        )
    ).all()

    name_map = _build_topic_name_map()
    result: list[tuple[str, int]] = []
    for topic_id, total in rows:
        name = name_map.get(topic_id, f"тема #{topic_id}")
        if name in _SKIP_TOPIC_NAMES:
            continue
        result.append((name, int(total)))

    return result[:8]


def _format_stats_block(stats: list[tuple[str, int]]) -> str:
    if not stats:
        return "Активность за неделю: нет данных."
    lines = ["Активность за неделю:"]
    for name, count in stats:
        lines.append(f"  • {name}: {count} сообщений")
    return "\n".join(lines)


async def send_weekly_digest(bot: Bot) -> None:
    """Отправляет еженедельный дайджест в топик «Важное» по воскресеньям (20:00)."""
    target_topic = settings.topic_important
    if target_topic is None:
        logger.info("Недельный дайджест пропущен: topic_important не задан.")
        return

    try:
        # Собираем данные параллельно
        stats_list: list[tuple[str, int]] = []
        async for session in get_session():
            stats_list = await _get_weekly_stats(session)

        weather, holidays = await asyncio.gather(
            fetch_weather(),
            fetch_holidays(),
            return_exceptions=True,
        )
        if isinstance(weather, BaseException):
            weather = ""
        if isinstance(holidays, BaseException):
            holidays = ""

        stats_block = _format_stats_block(stats_list)
        user_message = (
            f"{stats_block}\n"
            f"Погода сейчас: {weather or 'данные недоступны'}\n"
            f"Праздники: {holidays or 'нет'}\n"
            "Напиши еженедельный дайджест для жителей."
        )

        content: str | None = None
        try:
            ai_client = get_ai_client()
            provider = ai_client._provider
            if hasattr(provider, "_chat_completion"):
                llm_content, _ = await provider._chat_completion(
                    [
                        {"role": "system", "content": _DIGEST_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    chat_id=settings.forum_chat_id,
                    bypass_limit=True,
                )
                if llm_content and llm_content.strip():
                    content = llm_content.strip()[:800]
        except Exception:
            logger.warning("Недельный дайджест: ошибка LLM, fallback.", exc_info=True)

        if not content:
            total = sum(c for _, c in stats_list)
            top_names = ", ".join(n for n, _ in stats_list[:3]) if stats_list else "—"
            content = (
                f"Итоги недели в ЖК «Живописный»:\n"
                f"Всего сообщений: {total}\n"
                f"Самые активные разделы: {top_names}\n"
                f"Хороших выходных!"
            )

        await bot.send_message(
            settings.forum_chat_id,
            content[:900],
            message_thread_id=target_topic,
        )
        logger.info("WEEKLY_DIGEST: отправлен в topic=%s (%d топиков в статистике)", target_topic, len(stats_list))

    except Exception:
        logger.warning("Не удалось отправить недельный дайджест.", exc_info=True)
