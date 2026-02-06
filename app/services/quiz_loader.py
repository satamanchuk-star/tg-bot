"""Почему: единый источник вопросов — локальный XLSX-файл викторины."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx
from aiogram import Bot
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import QuizQuestion

BASE_URL = "https://quizvopros.ru/"
GOTQUESTIONS_URL = "https://gotquestions.online/"
MAX_PAGES = 15
MAX_GOTQUESTIONS_PAGES = 10
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
QUIZ_XLSX_PATH = Path(__file__).resolve().parents[2] / "viktorinavopros_QA.xlsx"
_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for char in letters:
        idx = idx * 26 + (ord(char) - ord("A") + 1)
    return idx


def _read_xlsx_questions(path: Path) -> list[tuple[str, str]]:
    """Читает пары вопрос/ответ из XLSX (колонки A/B)."""
    with ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("x:si", _NS):
                text_parts = [node.text or "" for node in item.findall(".//x:t", _NS)]
                shared_strings.append("".join(text_parts))

        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(".//x:sheetData/x:row", _NS)

    questions: list[tuple[str, str]] = []
    for row in rows[1:]:  # пропускаем заголовок
        values: dict[int, str] = {}
        for cell in row.findall("x:c", _NS):
            cell_ref = cell.get("r", "")
            col_idx = _column_index(cell_ref)
            value_node = cell.find("x:v", _NS)
            if value_node is None:
                continue
            value = value_node.text or ""
            if cell.get("t") == "s" and value.isdigit():
                shared_idx = int(value)
                if 0 <= shared_idx < len(shared_strings):
                    value = shared_strings[shared_idx]
            values[col_idx] = " ".join(value.split())

        question = values.get(1, "")
        answer = values.get(2, "")
        if not question or not answer:
            continue
        if question.lower() == "вопрос" and answer.lower() == "ответ":
            continue
        questions.append((question, answer))
    return questions


async def load_questions_from_xlsx() -> AsyncGenerator[str, None]:
    """Загружает вопросы из viktorinavopros_QA.xlsx рядом с проектом."""
    yield "Читаю вопросы из viktorinavopros_QA.xlsx..."
    if not QUIZ_XLSX_PATH.exists():
        yield "DONE"
        return

    questions = _read_xlsx_questions(QUIZ_XLSX_PATH)
    if not questions:
        yield "DONE"
        return

    parts = ["DONE"]
    for question, answer in questions:
        parts.append(question)
        parts.append(answer)
    yield "|".join(parts)


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


def _extract_question_answer_blocks(text: str) -> list[tuple[str, str]]:
    """Ищет пары вопрос/ответ в свободном тексте."""
    patterns = [
        r"(?:вопрос|question)\s*[:\-–]\s*(.+?)\s*(?:ответ|answer)\s*[:\-–]\s*(.+?)(?=(?:вопрос|question)\s*[:\-–]|\Z)",
        r"(?:вопрос|question)\s*\d*\s*[:\-–]\s*(.+?)\s*(?:ответ|answer)\s*\d*\s*[:\-–]\s*(.+?)(?=(?:вопрос|question)\s*\d*\s*[:\-–]|\Z)",
    ]
    questions: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in re.findall(pattern, text, re.IGNORECASE | re.DOTALL):
            question = " ".join(match[0].split())
            answer = " ".join(match[1].split())
            if 10 < len(question) < 500 and 1 < len(answer) < 200:
                questions.append((question, answer))
        if questions:
            break
    return questions


def _extract_questions(text: str) -> list[tuple[str, str]]:
    """Извлекает вопросы/ответы из текста с fallback-паттернами."""
    questions = _extract_question_answer_blocks(text)
    if questions:
        return questions
    return _extract_questions_from_text(text)


def _parse_questions_page(html: str) -> list[tuple[str, str]]:
    """Парсит страницу с вопросами."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, nav, header, footer, .sidebar, .comments"):
        tag.decompose()

    content = soup.select_one("article, .entry-content, .post-content, main")
    text = (
        content.get_text(separator="\n") if content else soup.get_text(separator="\n")
    )

    return _extract_questions(text)


def _get_article_links(
    html: str,
    base_url: str,
    include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> list[str]:
    """Извлекает ссылки на статьи с вопросами."""
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    include_keywords = include_keywords or []
    exclude_keywords = exclude_keywords or []

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if base_url in href or href.startswith("/"):
            full_url = urljoin(base_url, href)
            lower_url = full_url.lower()
            if any(
                x in lower_url
                for x in [
                    "/category/",
                    "/tag/",
                    "/page/",
                    "/author/",
                    "#",
                    "?",
                    ".jpg",
                    ".png",
                ]
            ):
                continue
            if exclude_keywords and any(x in lower_url for x in exclude_keywords):
                continue
            if include_keywords and not any(x in lower_url for x in include_keywords):
                continue
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

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
    ) as client:
        yield "Сбор ссылок на страницы..."

        # Главная страница
        html = await _fetch_page(client, BASE_URL)
        if html:
            all_links.update(
                _get_article_links(
                    html,
                    BASE_URL,
                    include_keywords=["вопрос", "квиз", "интеллектуальн"],
                )
            )

        # Пагинация
        for page in range(2, MAX_PAGES + 1):
            url = f"{BASE_URL}page/{page}/"
            html = await _fetch_page(client, url)
            if not html:
                break
            links = _get_article_links(
                html,
                BASE_URL,
                include_keywords=["вопрос", "квиз", "интеллектуальн"],
            )
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


async def load_questions_from_gotquestions() -> AsyncGenerator[str, None]:
    """Загружает вопросы с gotquestions.online, yield'ит сообщения о прогрессе."""
    all_questions: list[tuple[str, str]] = []
    seen_questions: set[str] = set()
    all_links: set[str] = set()

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
    ) as client:
        yield "Сбор ссылок gotquestions.online..."

        html = await _fetch_page(client, GOTQUESTIONS_URL)
        if html:
            all_links.update(
                _get_article_links(
                    html,
                    GOTQUESTIONS_URL,
                    include_keywords=["question", "вопрос", "quiz"],
                    exclude_keywords=["/page/"],
                )
            )

        for page in range(2, MAX_GOTQUESTIONS_PAGES + 1):
            url = f"{GOTQUESTIONS_URL}page/{page}/"
            html = await _fetch_page(client, url)
            if not html:
                break
            links = _get_article_links(
                html,
                GOTQUESTIONS_URL,
                include_keywords=["question", "вопрос", "quiz"],
                exclude_keywords=["/page/"],
            )
            if not links:
                break
            all_links.update(links)
            await asyncio.sleep(0.3)

        yield f"Найдено {len(all_links)} страниц с вопросами (gotquestions.online)"

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
                yield f"[{i}/{len(all_links)}] +{new_count} вопросов (gotquestions.online)"

            await asyncio.sleep(0.4)

    if all_questions:
        parts = ["DONE"]
        for q, a in all_questions:
            parts.append(q)
            parts.append(a)
        yield "|".join(parts)
    else:
        yield "DONE"


async def collect_questions(
    loader: AsyncGenerator[str, None],
) -> list[tuple[str, str]]:
    """Собирает вопросы из loader-генератора."""
    questions: list[tuple[str, str]] = []
    async for progress in loader:
        if progress.startswith("DONE"):
            parts = progress.split("|")
            if len(parts) > 1:
                for i in range(1, len(parts) - 1, 2):
                    questions.append((parts[i], parts[i + 1]))
    return questions


async def save_questions_to_db(
    session: AsyncSession,
    questions: list[tuple[str, str]],
) -> int:
    """Сохраняет вопросы в БД, пропуская дубликаты. Возвращает кол-во добавленных."""
    result = await session.execute(select(QuizQuestion.question))
    existing = {row[0] for row in result.fetchall()}
    existing_normalized = {_normalize_question(question) for question in existing}

    added = 0
    for question, answer in questions:
        normalized = _normalize_question(question)
        if normalized in existing_normalized:
            continue
        session.add(QuizQuestion(question=question, answer=answer))
        existing.add(question)
        existing_normalized.add(normalized)
        added += 1

    await session.commit()
    return added


def _normalize_question(text: str) -> str:
    """Нормализует текст вопроса для проверки дубликатов."""
    return " ".join(text.lower().split())


async def auto_load_quiz_questions(bot: Bot) -> None:
    """Автозагружает вопросы из XLSX-файла и логирует результат."""
    sources = [("viktorinavopros_QA.xlsx", load_questions_from_xlsx)]
    all_questions: list[tuple[str, str]] = []
    source_stats: list[tuple[str, int]] = []

    for name, loader_factory in sources:
        questions = await collect_questions(loader_factory())
        source_stats.append((name, len(questions)))
        all_questions.extend(questions)

    if not all_questions:
        await bot.send_message(
            settings.admin_log_chat_id,
            "Автозагрузка викторины: вопросы не найдены.",
        )
        return

    async for session in get_session():
        added = await save_questions_to_db(session, all_questions)

    details = "\n".join(f"• {name}: найдено {count}" for name, count in source_stats)
    await bot.send_message(
        settings.admin_log_chat_id,
        "Автозагрузка викторины завершена.\n"
        f"Найдено всего: {sum(count for _, count in source_stats)}\n"
        f"Добавлено новых: {added}\n"
        f"{details}",
    )
