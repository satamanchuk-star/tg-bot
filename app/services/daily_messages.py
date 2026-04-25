"""Ежедневные сообщения: утреннее приветствие с погодой/праздниками."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from bs4 import BeautifulSoup

from app.config import settings
from app.services.ai_module import get_ai_client

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 10
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Координаты ЖК Живописный (Бутово)
_ZHK_LAT, _ZHK_LON = 55.5697, 37.5419

# Дни недели на русском
_WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

# ---------------------------------------------------------------------------
# Системные промпты
# ---------------------------------------------------------------------------

_MORNING_GREETING_SYSTEM_PROMPT = (
    "Ты — дружелюбный бот жилого комплекса «Живописный» (Бутово).\n"
    "Напиши утреннее приветствие для соседей (3-5 предложений).\n"
    "Включи информацию о погоде, если она есть в контексте.\n"
    "Упомяни 1-2 праздника дня, если они указаны.\n"
    "Дай практичный совет по погоде (зонт, куртка, солнцезащитные очки и т.д.).\n"
    "Стиль: тёплый, живой, как сосед который выглянул в окно и делится впечатлениями.\n"
    "Каждый раз пиши по-разному — не повторяй шаблоны.\n"
    "1-2 эмодзи максимум. Разговорный русский. НЕ пиши длинных текстов."
)


# ---------------------------------------------------------------------------
# Скрапинг данных
# ---------------------------------------------------------------------------

async def fetch_weather() -> str:
    """Получает текущую погоду через wttr.in JSON API."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_FETCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                f"https://wttr.in/{_ZHK_LAT},{_ZHK_LON}",
                params={"format": "j1", "lang": "ru"},
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()

        data = resp.json()
        current = data.get("current_condition", [{}])[0]
        temp = current.get("temp_C", "?")
        feels = current.get("FeelsLikeC", "?")
        humidity = current.get("humidity", "?")

        # Описание погоды на русском
        desc_list = current.get("lang_ru", [{}])
        if desc_list:
            desc = desc_list[0].get("value", "")
        else:
            desc_en = current.get("weatherDesc", [{}])
            desc = desc_en[0].get("value", "") if desc_en else ""

        # Прогноз на день
        forecast = ""
        weather_list = data.get("weather", [])
        if weather_list:
            today = weather_list[0]
            max_temp = today.get("maxtempC", "?")
            min_temp = today.get("mintempC", "?")
            forecast = f" Днём {min_temp}..{max_temp}°C."

        result = f"{temp}°C, {desc}, ощущается как {feels}°C, влажность {humidity}%.{forecast}"
        logger.info("Погода получена: %s", result)
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Не удалось получить погоду с wttr.in: %s", exc)
    except Exception:
        logger.exception("Ошибка при получении погоды.")
    return ""


async def fetch_holidays() -> str:
    """Получает праздники дня с kakoysegodnyaprazdnik.ru."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_FETCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                "https://kakoysegodnyaprazdnik.ru/",
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept-Language": "ru-RU,ru;q=0.9",
                },
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        holidays: list[str] = []

        # Пробуем разные селекторы (структура сайта может меняться)
        for selector in (
            "div.listing_item a.title",
            "#content ul li a",
            ".mainPage .itemCard a",
            "div.listing a",
            "span.title",
        ):
            tags = soup.select(selector)
            if tags:
                for tag in tags[:5]:
                    name = tag.get_text(strip=True)
                    if name and len(name) > 3:
                        holidays.append(name)
                break

        # Фоллбек: ищем любые ссылки в основном контенте
        if not holidays:
            content_div = soup.find("div", {"id": "content"}) or soup.find("main") or soup.body
            if content_div:
                for a_tag in content_div.find_all("a", limit=20):
                    text = a_tag.get_text(strip=True)
                    if text and len(text) > 5 and "праздн" not in text.lower() and "день" in text.lower():
                        holidays.append(text)
                    if len(holidays) >= 5:
                        break

        result = ", ".join(holidays[:3])
        if result:
            logger.info("Праздники получены: %s", result)
        return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Не удалось получить праздники: %s", exc)
    except Exception:
        logger.exception("Ошибка при получении праздников.")
    return ""


# ---------------------------------------------------------------------------
# Отправка сообщений
# ---------------------------------------------------------------------------

async def send_morning_greeting(bot: Bot) -> None:
    """Отправляет утреннее приветствие с погодой и праздниками в General (8:00)."""
    if not settings.ai_daily_greeting:
        return

    try:
        weather, holidays = await asyncio.gather(
            fetch_weather(),
            fetch_holidays(),
            return_exceptions=True,
        )
        # Обработка исключений из gather
        if isinstance(weather, BaseException):
            logger.warning("Ошибка получения погоды: %s", weather)
            weather = ""
        if isinstance(holidays, BaseException):
            logger.warning("Ошибка получения праздников: %s", holidays)
            holidays = ""

        now = datetime.now(ZoneInfo(settings.timezone))
        weekday = _WEEKDAYS_RU[now.weekday()]
        date_str = now.strftime("%d.%m.%Y")

        user_message = (
            f"Дата: {date_str}, {weekday}\n"
            f"Погода: {weather or 'данные недоступны'}\n"
            f"Праздники сегодня: {holidays or 'обычный день, без праздников'}\n"
            "Напиши утреннее приветствие соседям."
        )

        ai_client = get_ai_client()
        provider = ai_client._provider
        if not hasattr(provider, "_chat_completion"):
            logger.info("Утреннее приветствие пропущено: нет remote AI провайдера.")
            return

        content, _ = await provider._chat_completion(
            [
                {"role": "system", "content": _MORNING_GREETING_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            chat_id=settings.forum_chat_id,
            temperature=0.6,
            bypass_limit=True,
        )

        if content and content.strip():
            await bot.send_message(
                settings.forum_chat_id,
                content.strip()[:800],
            )
            logger.info("DAILY_GREETING: утреннее приветствие отправлено в General")

    except Exception:
        logger.warning("Не удалось отправить утреннее приветствие.", exc_info=True)


async def send_daily_digest(bot: Bot) -> None:
    """Ежедневная AI-сводка сообщений форума (время задаётся AI_SUMMARY_HOUR/MINUTE)."""
    if not settings.ai_feature_daily_summary or not settings.ai_enabled:
        return

    from datetime import date
    from sqlalchemy import select
    from app.db import get_session
    from app.models import MessageLog

    tz = ZoneInfo(settings.timezone)
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=tz)

    rows: list[str] = []
    try:
        async for session in get_session():
            result = await session.execute(
                select(MessageLog.text)
                .where(
                    MessageLog.chat_id == settings.forum_chat_id,
                    MessageLog.created_at >= today_start,
                    MessageLog.severity <= 0,
                    MessageLog.text.isnot(None),
                )
                .order_by(MessageLog.created_at.asc())
                .limit(200)
            )
            rows = [r[0] for r in result.fetchall() if r[0] and r[0].strip()]
            break
    except Exception:
        logger.exception("send_daily_digest: ошибка при чтении сообщений из БД.")
        return

    if not rows:
        logger.info("send_daily_digest: нет сообщений за сегодня, пропускаем.")
        return

    context = "\n".join(rows)[:4000]
    try:
        summary = await get_ai_client().generate_daily_summary(
            context, chat_id=settings.forum_chat_id,
        )
    except Exception:
        logger.exception("send_daily_digest: ошибка AI.")
        return

    if not summary or not summary.strip():
        logger.warning("send_daily_digest: AI вернул пустой ответ.")
        return

    target_chat = settings.admin_log_chat_id
    thread_id = settings.ai_summary_topic_id
    try:
        if thread_id:
            await bot.send_message(target_chat, summary.strip(), message_thread_id=thread_id)
        else:
            await bot.send_message(target_chat, summary.strip())
        logger.info("send_daily_digest: сводка отправлена.")
    except Exception:
        logger.exception("send_daily_digest: не удалось отправить сводку.")


# ---------------------------------------------------------------------------
# Ежедневная картинка по главной теме курилки
# ---------------------------------------------------------------------------

_SMOKE_THEME_SYSTEM_PROMPT = (
    "Ты анализируешь сообщения из раздела 'Курилка' чата жилого комплекса. "
    "Найди ОДНУ самую обсуждаемую тему дня и сформулируй её как короткое образное "
    "описание для генерации картинки. "
    "Стиль: дружелюбный, с лёгким юмором, на русском языке. "
    "Ответь ТОЛЬКО описанием темы: 5-15 слов, без вводных фраз и пояснений."
)


async def send_smoke_topic_image(bot: Bot) -> None:
    """Раз в день определяет главную тему курилки и публикует тематическую картинку."""
    if not settings.ai_digest_image_enabled or not settings.ai_image_enabled:
        return
    if not settings.topic_smoke or not settings.ai_enabled:
        return

    from datetime import date
    from sqlalchemy import select
    from aiogram.types import BufferedInputFile
    from app.db import get_session
    from app.models import MessageLog
    from app.services.ai_image import generate_image, is_daily_limit_reached
    from app.services.ai_module import OpenRouterProvider

    tz = ZoneInfo(settings.timezone)
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=tz)

    # 1. Собираем сообщения из курилки за сегодня
    rows: list[str] = []
    try:
        async for session in get_session():
            result = await session.execute(
                select(MessageLog.text)
                .where(
                    MessageLog.chat_id == settings.forum_chat_id,
                    MessageLog.topic_id == settings.topic_smoke,
                    MessageLog.created_at >= today_start,
                    MessageLog.severity <= 0,
                    MessageLog.text.isnot(None),
                )
                .order_by(MessageLog.created_at.asc())
                .limit(150)
            )
            rows = [r[0] for r in result.fetchall() if r[0] and r[0].strip()]
            break
    except Exception:
        logger.exception("send_smoke_topic_image: ошибка чтения сообщений из БД.")
        return

    if not rows:
        logger.info("send_smoke_topic_image: нет сообщений в курилке за сегодня, пропускаем.")
        return

    if is_daily_limit_reached():
        logger.warning("send_smoke_topic_image: дневной лимит изображений исчерпан.")
        return

    # 2. AI определяет главную тему обсуждения
    context = "\n".join(rows)[:3000]
    provider = get_ai_client()._provider  # noqa: SLF001
    if not isinstance(provider, OpenRouterProvider):
        logger.info("send_smoke_topic_image: AI в stub-режиме, пропускаем.")
        return

    try:
        theme, _ = await provider._chat_completion_with_model(  # noqa: SLF001
            settings.ai_classifier_model,
            [
                {"role": "system", "content": _SMOKE_THEME_SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            chat_id=settings.forum_chat_id,
            max_tokens=60,
            temperature=0.5,
        )
        theme = theme.strip()[:200]
    except Exception:
        logger.exception("send_smoke_topic_image: ошибка AI при определении темы.")
        return

    if not theme:
        logger.warning("send_smoke_topic_image: AI не определил тему.")
        return

    logger.info("send_smoke_topic_image: тема='%s'", theme)

    # 3. Генерируем картинку
    try:
        image_bytes = await generate_image(
            "smoke", theme,
            chat_id=settings.forum_chat_id,
            user_id=None,
        )
    except RuntimeError:
        logger.exception("send_smoke_topic_image: ошибка генерации картинки.")
        return

    # 4. Отправляем в топик курилки
    caption = f"Главная тема курилки сегодня: {theme}"
    try:
        await bot.send_photo(
            settings.forum_chat_id,
            photo=BufferedInputFile(image_bytes, "smoke_image.png"),
            caption=caption,
            message_thread_id=settings.topic_smoke,
        )
        logger.info("send_smoke_topic_image: картинка отправлена в курилку.")
    except Exception:
        logger.exception("send_smoke_topic_image: не удалось отправить картинку.")
