#!/usr/bin/env python3
"""Скрипт для загрузки вопросов в базу данных из iqga.me/base/."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в путь для импорта app
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from bs4 import BeautifulSoup

from app.db import Base, engine, get_session
from app.models import QuizQuestion


BASE_URL = "https://iqga.me/base/"
MAX_PAGES = 50


async def init_db() -> None:
    """Создаёт таблицы если не существуют."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def fetch_page(client: httpx.AsyncClient, page: int) -> str | None:
    """Загружает страницу с вопросами."""
    url = f"{BASE_URL}?page={page}" if page > 1 else BASE_URL
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as e:
        print(f"Ошибка при загрузке страницы {page}: {e}")
        return None


def parse_questions(html: str) -> list[tuple[str, str]]:
    """Парсит вопросы и ответы из HTML страницы."""
    soup = BeautifulSoup(html, "html.parser")
    questions = []

    # Ищем блоки с вопросами
    for item in soup.select(".question-item, .quiz-item, article"):
        question_el = item.select_one(".question-text, .question, h2, h3")
        answer_el = item.select_one(".answer-text, .answer, .spoiler")

        if question_el and answer_el:
            question = question_el.get_text(strip=True)
            answer = answer_el.get_text(strip=True)
            if question and answer:
                questions.append((question, answer))

    # Альтернативный парсинг для таблиц
    for row in soup.select("table tr"):
        cells = row.select("td")
        if len(cells) >= 2:
            question = cells[0].get_text(strip=True)
            answer = cells[1].get_text(strip=True)
            if question and answer and len(question) > 10:
                questions.append((question, answer))

    return questions


async def load_questions_from_web() -> list[tuple[str, str]]:
    """Загружает все вопросы с сайта."""
    all_questions = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(1, MAX_PAGES + 1):
            print(f"Загрузка страницы {page}...")
            html = await fetch_page(client, page)
            if not html:
                break

            questions = parse_questions(html)
            if not questions:
                print(f"Страница {page}: вопросов не найдено, завершаем")
                break

            all_questions.extend(questions)
            print(f"Страница {page}: найдено {len(questions)} вопросов")

            # Небольшая пауза чтобы не нагружать сервер
            await asyncio.sleep(0.5)

    return all_questions


async def save_questions(questions: list[tuple[str, str]]) -> int:
    """Сохраняет вопросы в БД, пропуская дубликаты."""
    saved = 0

    async for session in get_session():
        # Получаем существующие вопросы
        from sqlalchemy import select
        result = await session.execute(select(QuizQuestion.question))
        existing = {row[0] for row in result.fetchall()}

        for question, answer in questions:
            if question not in existing:
                session.add(QuizQuestion(question=question, answer=answer))
                existing.add(question)
                saved += 1

        await session.commit()

    return saved


async def main() -> None:
    print("Инициализация БД...")
    await init_db()

    print("\nЗагрузка вопросов с iqga.me...")
    questions = await load_questions_from_web()

    if not questions:
        print("\nВопросы не найдены. Проверьте структуру сайта.")
        print("Добавляем тестовые вопросы...")

        # Добавляем несколько тестовых вопросов
        questions = [
            ("Столица России?", "Москва"),
            ("Сколько планет в Солнечной системе?", "8"),
            ("Кто написал 'Войну и мир'?", "Толстой"),
            ("В каком году началась Вторая мировая война?", "1939"),
            ("Самая длинная река в мире?", "Нил"),
            ("Химический символ золота?", "Au"),
            ("Кто изобрёл телефон?", "Белл"),
            ("Столица Японии?", "Токио"),
            ("Сколько сторон у треугольника?", "3"),
            ("Самое большое млекопитающее?", "Синий кит"),
        ]

    print(f"\nВсего вопросов: {len(questions)}")

    print("Сохранение в БД...")
    saved = await save_questions(questions)

    print(f"\nГотово! Сохранено {saved} новых вопросов.")


if __name__ == "__main__":
    asyncio.run(main())
