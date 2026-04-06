"""Ежедневные сообщения: утреннее приветствие с погодой/праздниками и трафик в Попутчиках."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
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

_MORNING_TRAFFIC_SYSTEM_PROMPT = (
    "Ты — дружелюбный бот жилого комплекса «Живописный» (Бутово).\n"
    "Напиши утренний отчёт о дорожной ситуации для соседей-попутчиков (2-4 предложения).\n"
    "Контекст: утро, люди едут на работу ИЗ ЖК до метро Аннино и до МКАД.\n"
    "Тон: бодрый утром, сочувственный если пробки, позитивный если свободно.\n"
    "Дай практичный совет: когда лучше выехать, стоит ли подождать.\n"
    "Каждый раз пиши по-разному. 1 эмодзи. Разговорный русский.\n"
    "Начни с короткого 'Доброе утро!' или аналога."
)

_EVENING_TRAFFIC_SYSTEM_PROMPT = (
    "Ты — дружелюбный бот жилого комплекса «Живописный» (Бутово).\n"
    "Напиши вечерний отчёт о дорожной ситуации для соседей-попутчиков (2-4 предложения).\n"
    "Контекст: вечер, люди едут домой В ЖК от метро Аннино и от МКАД.\n"
    "Тон: сочувственный если пробки, позитивный если свободно, тёплый вечерний.\n"
    "Дай практичный совет: стоит ли ещё подождать, или дороги уже свободнее.\n"
    "Каждый раз пиши по-разному. 1 эмодзи. Разговорный русский.\n"
    "Начни с короткого 'Добрый вечер!' или аналога."
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


async def fetch_traffic_score() -> str:
    """Получает балл пробок Москвы из Яндекс-информера."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_FETCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                "https://export.yandex.ru/bar/reginfo.xml",
                params={"region": "213"},
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()

        root = ET.fromstring(resp.text)
        traffic_el = root.find(".//traffic")
        if traffic_el is not None:
            level = traffic_el.findtext("level", "")
            hint = traffic_el.findtext("hint", "") or traffic_el.findtext("text", "")
            if level:
                result = f"Пробки в Москве: {level}/10"
                if hint:
                    result += f" ({hint})"
                logger.info("Трафик получен: %s", result)
                return result

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Не удалось получить трафик из Яндекс-информера: %s", exc)
    except Exception:
        logger.exception("Ошибка при получении трафика.")
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
        )

        if content and content.strip():
            await bot.send_message(
                settings.forum_chat_id,
                content.strip()[:800],
            )
            logger.info("DAILY_GREETING: утреннее приветствие отправлено в General")

    except Exception:
        logger.warning("Не удалось отправить утреннее приветствие.")


async def send_traffic_report(bot: Bot, period: str) -> None:
    """Отправляет отчёт о пробках в Попутчики (7:00 утро / 19:00 вечер).

    period: 'morning' или 'evening'
    """
    if not settings.ai_traffic_report:
        return
    if settings.topic_rides is None:
        logger.info("Трафик-отчёт пропущен: topic_rides не задан.")
        return

    try:
        traffic = await fetch_traffic_score()

        now = datetime.now(ZoneInfo(settings.timezone))
        weekday = _WEEKDAYS_RU[now.weekday()]

        if period == "morning":
            system_prompt = _MORNING_TRAFFIC_SYSTEM_PROMPT
            direction_info = (
                f"День: {weekday}\n"
                f"Дорожная обстановка: {traffic or 'данные недоступны'}\n"
                "Направления: из ЖК «Живописный» до метро Аннино и из ЖК до МКАД.\n"
                "Напиши утренний отчёт о пробках для попутчиков."
            )
        else:
            system_prompt = _EVENING_TRAFFIC_SYSTEM_PROMPT
            direction_info = (
                f"День: {weekday}\n"
                f"Дорожная обстановка: {traffic or 'данные недоступны'}\n"
                "Направления: от метро Аннино до ЖК «Живописный» и от МКАД до ЖК.\n"
                "Напиши вечерний отчёт о пробках для попутчиков."
            )

        ai_client = get_ai_client()
        provider = ai_client._provider
        if not hasattr(provider, "_chat_completion"):
            logger.info("Трафик-отчёт пропущен: нет remote AI провайдера.")
            return

        content, _ = await provider._chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": direction_info},
            ],
            chat_id=settings.forum_chat_id,
        )

        if content and content.strip():
            await bot.send_message(
                settings.forum_chat_id,
                content.strip()[:600],
                message_thread_id=settings.topic_rides,
            )
            logger.info("TRAFFIC_REPORT: %s отчёт отправлен в Попутчики", period)

    except Exception:
        logger.warning("Не удалось отправить трафик-отчёт (%s).", period)
