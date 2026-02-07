"""Почему: загружаем единый официальный набор вопросов викторины из XLSX в БД."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from aiogram import Bot
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizQuestion

QUIZ_XLSX_PATH = Path(__file__).resolve().parents[2] / "viktorinavopros_QA.xlsx"
QUIZ_TEXT_PATH = Path(__file__).resolve().parents[1] / "data" / "quiz_questions.txt"
_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _read_text_questions(path: Path) -> list[tuple[str, str]]:
    """Совместимость с тестами: читаем пары вопрос/ответ из текстового файла."""
    questions: list[tuple[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = next((sep for sep in ("|", ";", "\t") if sep in line), None)
        if separator is None:
            continue
        question, answer = (part.strip() for part in line.split(separator, 1))
        if question and answer:
            questions.append((" ".join(question.split()), " ".join(answer.split())))
    return questions


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for char in letters:
        idx = idx * 26 + (ord(char) - ord("A") + 1)
    return idx


def _read_xlsx_questions(path: Path) -> list[tuple[str, str]]:
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
    for row in rows[1:]:
        values: dict[int, str] = {}
        for cell in row.findall("x:c", _NS):
            col_idx = _column_index(cell.get("r", ""))
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


def _normalize_question(text: str) -> str:
    return " ".join(text.lower().split())


async def sync_questions_from_xlsx(session: AsyncSession) -> tuple[int, int]:
    if not QUIZ_XLSX_PATH.exists():
        return 0, 0

    source = _read_xlsx_questions(QUIZ_XLSX_PATH)
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for question, answer in source:
        normalized = _normalize_question(question)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append((question, answer))

    await session.execute(delete(QuizQuestion))
    for question, answer in unique:
        session.add(QuizQuestion(question=question, answer=answer))
    await session.commit()
    return len(source), len(unique)


async def sync_questions_from_text(session: AsyncSession) -> tuple[int, int]:
    """Совместимость: старый вызов теперь синхронизирует из XLSX."""
    return await sync_questions_from_xlsx(session)


async def auto_load_quiz_questions(bot: Bot) -> None:
    from app.config import settings
    from app.db import get_session

    async for session in get_session():
        total, unique = await sync_questions_from_xlsx(session)
        break

    if total == 0:
        await bot.send_message(
            settings.admin_log_chat_id,
            "Автозагрузка викторины: файл viktorinavopros_QA.xlsx не найден или пуст.",
        )
        return

    await bot.send_message(
        settings.admin_log_chat_id,
        "Автозагрузка викторины завершена.\n"
        "Источник: viktorinavopros_QA.xlsx\n"
        f"Прочитано строк: {total}\n"
        f"Уникальных вопросов в БД: {unique}",
    )
