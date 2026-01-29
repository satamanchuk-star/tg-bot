"""Сервис загрузки вопросов с quizvopros.ru."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizQuestion

BASE_URL = "https://quizvopros.ru/"
MAX_PAGES = 15


async def _fetch_page(client: httpx.AsyncClient, url: str) -> str | None:
    """Загружает страницу."""
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError:
        return None


def _extract_questions_from_text(text: str) -> list[tuple[str, str]]:
    """Извлекает вопросы и ответы из текста страницы."""
    questions = []
    text_lower = text.lower()

    # Ищем маркер раздела ответов
    answer_markers = ["ответы", "ответы:", "ответы на вопросы"]
    answer_start = -1
    for marker in answer_markers:
        pos = text_lower.rfind(marker)
        if pos > answer_start:
            answer_start = pos

    if answer_start == -1:
        return questions

    questions_text = text[:answer_start]
    answers_text = text[answer_start:]

    # Паттерн для нумерованных пунктов
    pattern = r"(\d+)[.\)]\s*(.+?)(?=\d+[.\)]|\Z)"

    q_matches = re.findall(pattern, questions_text, re.DOTALL)
    q_dict = {int(num): txt.strip() for num, txt in q_matches if txt.strip()}

    a_matches = re.findall(pattern, answers_text, re.DOTALL)
    a_dict = {int(num): txt.strip() for num, txt in a_matches if txt.strip()}

    for num in sorted(q_dict.keys()):
        if num in a_dict:
            question = " ".join(q_dict[num].split())
            answer = " ".join(a_dict[num].split())
            if 10 < len(question) < 500 and 1 < len(answer) < 200:
                questions.append((question, answer))

    return questions


def _parse_questions_page(html: str) -> list[tuple[str, str]]:
    """Парсит страницу с вопросами."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, nav, header, footer, .sidebar, .comments"):
        tag.decompose()

    content = soup.select_one("article, .entry-content, .post-content, main")
    text = content.get_text(separator="\n") if content else soup.get_text(separator="\n")

    return _extract_questions_from_text(text)


def _get_article_links(html: str, base_url: str) -> list[str]:
    """Извлекает ссылки на статьи с вопросами."""
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "quizvopros.ru/" in href or href.startswith("/"):
            full_url = urljoin(base_url, href)
            if any(x in full_url.lower() for x in ["/category/", "/tag/", "/page/", "/author/", "#", "?", ".jpg", ".png"]):
                continue
            if "вопрос" in full_url.lower() or "квиз" in full_url.lower() or "интеллектуальн" in full_url.lower():
                links.add(full_url)

    return list(links)


async def load_questions_from_quizvopros() -> AsyncGenerator[str, None]:
    """Загружает вопросы с quizvopros.ru, yield'ит сообщения о прогрессе.

    Последнее сообщение содержит итоговый список вопросов в формате:
    "DONE|question1|answer1|question2|answer2|..."
    """
    all_questions: list[tuple[str, str]] = []
    seen_questions: set[str] = set()
    all_links: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        yield "Сбор ссылок на страницы..."

        # Главная страница
        html = await _fetch_page(client, BASE_URL)
        if html:
            all_links.update(_get_article_links(html, BASE_URL))

        # Пагинация
        for page in range(2, MAX_PAGES + 1):
            url = f"{BASE_URL}page/{page}/"
            html = await _fetch_page(client, url)
            if not html:
                break
            links = _get_article_links(html, BASE_URL)
            if not links:
                break
            all_links.update(links)
            await asyncio.sleep(0.3)

        yield f"Найдено {len(all_links)} страниц с вопросами"

        # Парсим каждую страницу
        for i, url in enumerate(all_links, 1):
            html = await _fetch_page(client, url)
            if not html:
                continue

            questions = _parse_questions_page(html)
            new_count = 0
            for q, a in questions:
                if q not in seen_questions:
                    seen_questions.add(q)
                    all_questions.append((q, a))
                    new_count += 1

            if new_count > 0:
                yield f"[{i}/{len(all_links)}] +{new_count} вопросов"

            await asyncio.sleep(0.5)

    # Финальное сообщение с данными
    if all_questions:
        parts = ["DONE"]
        for q, a in all_questions:
            parts.append(q)
            parts.append(a)
        yield "|".join(parts)
    else:
        yield "DONE"


async def save_questions_to_db(
    session: AsyncSession,
    questions: list[tuple[str, str]],
) -> int:
    """Сохраняет вопросы в БД, пропуская дубликаты. Возвращает кол-во добавленных."""
    result = await session.execute(select(QuizQuestion.question))
    existing = {row[0] for row in result.fetchall()}

    added = 0
    for question, answer in questions:
        if question not in existing:
            session.add(QuizQuestion(question=question, answer=answer))
            existing.add(question)
            added += 1

    await session.commit()
    return added
