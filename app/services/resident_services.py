"""Сервис каталога услуг от жителей ЖК.

Почему: жители предлагают услуги (кондитерская, ремонт, репетиторство и т.д.),
бот систематизирует их и подсказывает при запросах из любого топика.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ResidentService

logger = logging.getLogger(__name__)

# Стоп-слова для нормализации поисковых запросов
_STOP_WORDS = {
    "это", "как", "что", "когда", "где", "или", "для", "если", "чтобы",
    "можно", "нужно", "через", "просто", "только", "очень", "всем",
    "тут", "там", "про", "под", "над", "без", "еще", "уже", "тоже",
    "есть", "нет", "кто", "кто-то", "кто-нибудь", "занимается",
    "делает", "делаю", "может", "могу", "хочу", "ищу", "нужен", "нужна",
    "нас", "нам", "наш", "наша", "наши", "свой", "этот", "эта",
    "мне", "меня", "они", "она", "его", "ваш",
}

# Категории услуг и связанные ключевые слова
_SERVICE_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("кондитерская", ("торт", "пирог", "выпечк", "кондитер", "десерт", "пирожн", "шоколад",
                       "конфет", "капкейк", "мусс", "печень", "маффин", "кекс", "клубник")),
    ("красота", ("маникюр", "педикюр", "косметолог", "массаж", "визажист", "стилист",
                  "парикмахер", "стрижк", "ноготк", "наращиван", "брови", "ресниц",
                  "депиляц", "эпиляц", "макияж")),
    ("ремонт", ("ремонт", "сантехник", "электрик", "плитк", "штукатур", "покраск",
                 "поклейк", "обои", "ламинат", "плотник", "сборк", "мебел", "монтаж",
                 "установк", "кондиционер")),
    ("обучение", ("репетитор", "урок", "обучен", "курс", "английск", "математик",
                   "програм", "музык", "рисован", "подготовк", "егэ", "огэ")),
    ("дети", ("няня", "присмотр", "аниматор", "детск", "логопед", "дефектолог",
              "развива")),
    ("авто", ("автомобил", "эвакуатор", "шиномонтаж", "мойк", "детейлинг",
              "полировк", "тониров")),
    ("здоровье", ("врач", "медсестр", "психолог", "остеопат", "кинезиолог",
                   "логопед", "стоматолог", "ветеринар")),
    ("уборка", ("уборк", "клининг", "химчистк", "стирк", "глажк")),
    ("доставка", ("доставк", "курьер", "перевозк", "грузоперевозк", "переезд")),
    ("фото_видео", ("фотограф", "видеограф", "съёмк", "съемк", "фотосесси")),
    ("IT", ("компьютер", "ноутбук", "настройк", "сайт", "дизайн", "верстк")),
    ("юридические", ("юрист", "документ", "консультац", "нотариус", "бухгалтер")),
    ("рукоделие", ("шить", "шитьё", "вязан", "вышивк", "handmade", "хэндмейд",
                    "украшен", "букет", "цветы", "цветов", "флорист")),
]


def classify_service(text: str) -> str:
    """Определяет категорию услуги по тексту."""
    lowered = text.lower().replace("ё", "е")
    tokens = set(re.findall(r"[а-яёa-z0-9]+", lowered))
    for category, markers in _SERVICE_CATEGORIES:
        if any(any(token.startswith(m) for token in tokens) for m in markers):
            return category
    return "общее"


def extract_keywords(text: str) -> str:
    """Извлекает ключевые слова из текста услуги для поиска."""
    lowered = text.lower().replace("ё", "е")
    tokens = re.findall(r"[а-яёa-z0-9]+", lowered)
    meaningful = [t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS]
    # Убираем дубли, сохраняя порядок
    seen: set[str] = set()
    unique: list[str] = []
    for t in meaningful:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return ",".join(unique[:30])


async def add_service(
    session: AsyncSession,
    *,
    chat_id: int,
    message_text: str,
    provider_user_id: int,
    provider_name: str | None = None,
    source_message_id: int | None = None,
    added_by_user_id: int,
    ai_description: str | None = None,
    ai_keywords: str | None = None,
    ai_category: str | None = None,
) -> ResidentService:
    """Добавляет услугу в каталог."""
    description = ai_description or message_text[:500]
    keywords = ai_keywords or extract_keywords(message_text)
    category = ai_category or classify_service(message_text)

    record = ResidentService(
        chat_id=chat_id,
        message_text=message_text.strip()[:2000],
        description=description.strip()[:500],
        keywords=keywords[:1000],
        category=category,
        provider_user_id=provider_user_id,
        provider_name=provider_name,
        source_message_id=source_message_id,
        added_by_user_id=added_by_user_id,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.flush()
    return record


async def search_services(
    session: AsyncSession,
    chat_id: int,
    query: str,
    *,
    top_k: int = 5,
) -> list[ResidentService]:
    """Ищет услуги по запросу с использованием ключевых слов и категорий."""
    if not query or not query.strip():
        return []

    lowered = query.lower().replace("ё", "е")
    tokens = re.findall(r"[а-яёa-z0-9]+", lowered)
    search_tokens = [t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS]

    if not search_tokens:
        return []

    # Определяем подходящие категории
    matching_categories: list[str] = []
    for category, markers in _SERVICE_CATEGORIES:
        if any(any(token.startswith(m) or m.startswith(token[:4]) for token in search_tokens) for m in markers):
            matching_categories.append(category)

    # Строим условия поиска: по ключевым словам ИЛИ по тексту ИЛИ по категории
    word_conditions = []
    for token in search_tokens[:8]:
        like = f"%{token}%"
        word_conditions.append(
            or_(
                ResidentService.keywords.ilike(like),
                ResidentService.description.ilike(like),
                ResidentService.message_text.ilike(like),
            )
        )

    # Добавляем условие по категории
    if matching_categories:
        word_conditions.append(ResidentService.category.in_(matching_categories))

    if not word_conditions:
        return []

    result = await session.execute(
        select(ResidentService)
        .where(
            and_(
                ResidentService.chat_id == chat_id,
                ResidentService.is_active.is_(True),
                or_(*word_conditions),
            )
        )
        .order_by(ResidentService.created_at.desc())
        .limit(top_k)
    )
    services = list(result.scalars().all())

    # Ранжируем по количеству совпавших токенов
    def _relevance_score(svc: ResidentService) -> int:
        svc_text = f"{svc.keywords} {svc.description} {svc.category}".lower()
        score = sum(1 for t in search_tokens if t in svc_text)
        if svc.category in matching_categories:
            score += 2
        return score

    services.sort(key=_relevance_score, reverse=True)
    return services[:top_k]


def format_services_context(services: list[ResidentService]) -> str:
    """Форматирует найденные услуги для AI-контекста."""
    if not services:
        return ""
    parts: list[str] = []
    for idx, svc in enumerate(services, 1):
        line = f"[{idx}] ({svc.category}) {svc.description}"
        if svc.provider_name:
            line += f" — {svc.provider_name}"
        parts.append(line)
    return "\n".join(parts)


def format_services_for_user(services: list[ResidentService]) -> str:
    """Форматирует услуги для ответа пользователю."""
    if not services:
        return ""
    parts: list[str] = []
    for svc in services:
        line = f"• {svc.description}"
        if svc.provider_name:
            line += f" — {svc.provider_name}"
        parts.append(line)
    return "\n".join(parts)


async def get_services_count(session: AsyncSession, chat_id: int) -> int:
    """Количество активных услуг в каталоге."""
    from sqlalchemy import func
    result = await session.scalar(
        select(func.count())
        .select_from(ResidentService)
        .where(
            and_(
                ResidentService.chat_id == chat_id,
                ResidentService.is_active.is_(True),
            )
        )
    )
    return int(result or 0)


async def deactivate_service(session: AsyncSession, service_id: int) -> bool:
    """Деактивирует услугу."""
    svc = await session.get(ResidentService, service_id)
    if svc is None:
        return False
    svc.is_active = False
    await session.flush()
    return True
