"""Ежедневные сообщения: погода и праздники для недельного дайджеста."""

from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 10
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Координаты ЖК Живописный (Бутово)
_ZHK_LAT, _ZHK_LON = 55.5697, 37.5419

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


