"""Почему: даём боту доступ к интернету для ответов на вопросы вне базы знаний ЖК."""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 8
_MAX_RESULTS = 3
_MAX_SNIPPET_LEN = 300

# Паттерны вопросов, которые могут потребовать веб-поиска
_WEB_SEARCH_TRIGGERS = (
    "что такое",
    "кто такой",
    "кто такая",
    "как работает",
    "когда будет",
    "когда откроется",
    "когда закроется",
    "новости",
    "курс",
    "погода",
    "расписание",
    "маршрут",
    "адрес",
    "телефон",
    "сайт",
    "ссылка",
    "найди",
    "найти",
    "загугли",
    "погугли",
    "поищи",
    "в интернете",
    "в сети",
    "онлайн",
)

# Темы, которые НЕ стоит искать в интернете (обрабатываются локально)
_SKIP_WEB_PATTERNS = (
    "шлагбаум",
    "пропуск",
    "правила чата",
    "правила форума",
    "монеты",
    "игр",
    "блэкджек",
    "страйк",
    "мут",
    "бан",
)


def should_search_web(prompt: str) -> bool:
    """Определяет, стоит ли искать ответ в интернете."""
    if not settings.ai_feature_web_search:
        return False

    lowered = prompt.lower()

    # Пропускаем вопросы о внутренних делах бота/ЖК
    if any(p in lowered for p in _SKIP_WEB_PATTERNS):
        return False

    # Ищем триггеры веб-поиска
    if any(trigger in lowered for trigger in _WEB_SEARCH_TRIGGERS):
        return True

    # URL в вопросе — пользователь хочет что-то из интернета
    if re.search(r"https?://", lowered):
        return True

    return False


async def search_duckduckgo(query: str) -> list[dict[str, str]]:
    """Ищет в DuckDuckGo HTML и возвращает список результатов.

    Каждый результат: {"title": ..., "snippet": ..., "url": ...}
    """
    results: list[dict[str, str]] = []

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_SEARCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query[:200]},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for result_div in soup.select(".result"):
            if len(results) >= _MAX_RESULTS:
                break

            title_tag = result_div.select_one(".result__a")
            snippet_tag = result_div.select_one(".result__snippet")
            url_tag = result_div.select_one(".result__url")

            if not title_tag:
                continue

            title = title_tag.get_text(strip=True)
            snippet = snippet_tag.get_text(strip=True)[:_MAX_SNIPPET_LEN] if snippet_tag else ""
            url = ""
            if url_tag:
                url = url_tag.get_text(strip=True)
            if not url and title_tag.get("href"):
                url = str(title_tag["href"])

            if title:
                results.append({"title": title, "snippet": snippet, "url": url})

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Веб-поиск DuckDuckGo не удался: %s", exc)
    except Exception:
        logger.exception("Неожиданная ошибка при веб-поиске.")

    return results


async def fetch_page_text(url: str, max_chars: int = 2000) -> str:
    """Загружает веб-страницу и извлекает основной текст."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_SEARCH_TIMEOUT),
            follow_redirects=True,
        ) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Удаляем скрипты, стили, навигацию
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        # Убираем лишние пробелы
        text = re.sub(r"\s+", " ", text).strip()

        return text[:max_chars]

    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Не удалось загрузить страницу %s: %s", url, exc)
    except Exception:
        logger.exception("Ошибка при загрузке страницы %s", url)

    return ""


def format_search_context(results: list[dict[str, str]]) -> str:
    """Форматирует результаты поиска для контекста AI."""
    if not results:
        return ""

    lines = ["Результаты поиска в интернете:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   Источник: {r['url']}")
    lines.append(
        "\nИспользуй эти данные для ответа. Укажи источник, если цитируешь. "
        "Не выдумывай информацию, которой нет в результатах."
    )
    return "\n".join(lines)
