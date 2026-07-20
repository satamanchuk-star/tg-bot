"""Почему: сайты с вопросами недоступны из окружения сборки, но продакшн-сервер
в интернете. Поэтому импорт вопросов делаем в рантайме бота: скачиваем страницу
(httpx) и извлекаем пары «вопрос — ответ» его же ИИ (устойчиво к любой вёрстке).

Запускает админ командой /quiz_import <url>. Новые вопросы дедуплицируются и
падают в тот же пул quiz_questions, что и сид.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuizQuestion

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_MAX_PAGE_CHARS = 30_000  # ограничиваем ввод ИИ, чтобы не жечь токены
_EXTRACT_MAX_TOKENS = 4000
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_EXTRACT_PROMPT = (
    "Ниже текст веб-страницы с вопросами викторины. Извлеки пары «вопрос — ответ».\n\n"
    "Строгие правила:\n"
    "- Ответ КОРОТКИЙ и однозначный: 1–3 слова либо число/дата.\n"
    "- Отбрасывай вопросы, где ответ — предложение, объяснение, список или «зависит от…».\n"
    "- Отбрасывай вопросы про картинки/медиа.\n"
    "- Вопрос — самодостаточный текст на русском.\n"
    "- Если у ответа есть синонимы, объедини их через « / » (например «Пётр Первый / Пётр I»).\n"
    "- Убери нумерацию и префиксы «Вопрос:»/«Ответ:».\n\n"
    "Верни ТОЛЬКО JSON-массив объектов вида "
    '{"question": "...", "answer": "...", "category": "тема"} без markdown и пояснений.\n'
    "Если качественных пар нет — верни [].\n\n"
    "ТЕКСТ СТРАНИЦЫ:\n"
)


async def fetch_page_text(url: str) -> str:
    """Скачивает страницу и вытаскивает читаемый текст (без тегов/скриптов)."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_TIMEOUT), follow_redirects=True,
    ) as client:
        resp = await client.get(url, headers={"User-Agent": _UA})
        resp.raise_for_status()
        html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:_MAX_PAGE_CHARS]


def _parse_pairs(raw: str) -> list[dict]:
    """Разбирает ответ ИИ в список пар. Терпим к markdown-обёрткам."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", cleaned).strip()
    # На случай, если модель обернула массив в объект — вынимаем первый массив.
    if not cleaned.startswith("["):
        m = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
    try:
        data = json.loads(cleaned)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("questions") or data.get("items") or []
    pairs = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        a = str(item.get("answer", "")).strip()
        if q and a and len(a) <= 60:  # длинные ответы отсекаем ещё раз
            pairs.append({
                "question": q, "answer": a,
                "category": (str(item.get("category", "")).strip() or None),
            })
    return pairs


async def extract_qa_pairs(page_text: str, *, chat_id: int) -> list[dict]:
    """Извлекает пары «вопрос — ответ» из текста страницы силами ИИ бота."""
    if not page_text.strip():
        return []
    from app.config import settings
    from app.services.ai_module import get_ai_client

    client = get_ai_client()
    content, _tokens = await client._chat_completion_with_model(
        settings.ai_model,
        [{"role": "user", "content": _EXTRACT_PROMPT + page_text}],
        chat_id=chat_id,
        max_tokens=_EXTRACT_MAX_TOKENS,
        temperature=0.0,
    )
    return _parse_pairs(content)


def _key(question: str, answer: str) -> tuple[str, str]:
    return question.strip().lower(), answer.strip().lower()


async def insert_new_questions(session: AsyncSession, pairs: list[dict]) -> int:
    """Вставляет только новые пары (дедуп по вопрос+ответ). Возвращает число добавленных."""
    if not pairs:
        return 0
    existing = {
        _key(r.question, r.answer)
        for r in (await session.execute(select(QuizQuestion))).scalars().all()
    }
    added = 0
    for p in pairs:
        k = _key(p["question"], p["answer"])
        if k in existing:
            continue
        existing.add(k)
        session.add(QuizQuestion(
            question=p["question"], answer=p["answer"], category=p.get("category"),
        ))
        added += 1
    await session.flush()
    return added


async def import_from_url(session: AsyncSession, url: str, *, chat_id: int) -> tuple[int, int, int]:
    """Полный цикл: скачать → извлечь → вставить. Возврат (извлечено, добавлено, всего в базе)."""
    page = await fetch_page_text(url)
    pairs = await extract_qa_pairs(page, chat_id=chat_id)
    added = await insert_new_questions(session, pairs)
    total = int(await session.scalar(select(func.count()).select_from(QuizQuestion)) or 0)
    return len(pairs), added, total


# --- Одноразовый авто-импорт при старте (сайты владельца) ---

# Сайты с вопросами от владельца. При добавлении новых — поднять версию флага,
# чтобы деплой прошёлся по списку ещё раз.
AUTO_IMPORT_URLS = (
    "https://raznoeinteresnoe.ru/квиз-примеры-вопросов-с-ответами/",
    "https://olganevskaya.com/вопросы-от-сайта-квизбаза-архив/",
    "https://quizvopros.ru",
    "https://viktorinavopros.ru",
)
AUTO_IMPORT_FLAG = "quiz_autoimport_v1"


async def auto_import_startup(bot) -> None:
    """Фоновая задача старта: одноразово (под MigrationFlag) импортирует вопросы
    со всех сайтов владельца и отчитывается в админ-чат.

    Работает на сервере, где есть интернет (окружение сборки заблокировано).
    Ошибки отдельных сайтов не прерывают остальные.
    """
    from app.config import settings
    from app.db import get_session
    from app.models import MigrationFlag

    async for session in get_session():
        flag = await session.get(MigrationFlag, AUTO_IMPORT_FLAG)
        if flag is not None:
            return
        break
    else:
        return

    lines: list[str] = []
    total_added = 0
    total_in_base = 0
    for url in AUTO_IMPORT_URLS:
        try:
            async for session in get_session():
                extracted, added, total_in_base = await import_from_url(
                    session, url, chat_id=settings.admin_log_chat_id
                )
                await session.commit()
                break
            else:
                continue
            total_added += added
            lines.append(f"✅ {url[:55]} — извлечено {extracted}, новых {added}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("QUIZ autoimport: %s — %s", url, exc)
            lines.append(f"⚠️ {url[:55]} — {str(exc)[:80]}")

    # Флаг ставим после прохода (даже частично неудачного): повторный деплой не
    # будет долбить сайты; добить конкретный сайт можно вручную /quiz_import.
    async for session in get_session():
        session.add(MigrationFlag(key=AUTO_IMPORT_FLAG))
        await session.commit()
        break

    report = (
        f"🧠 Авто-импорт вопросов викторины: +{total_added} новых, "
        f"в базе {total_in_base}.\n\n" + "\n".join(lines)
        + "\n\nДобавить ещё: /quiz_import <ссылка>"
    )
    try:
        await bot.send_message(settings.admin_log_chat_id, report)
    except Exception:  # noqa: BLE001
        logger.info("QUIZ autoimport: отчёт не отправился.\n%s", report)
