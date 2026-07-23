"""Почему: петля роста качества — «не знаю»-вопросы копятся и закрываются админом.

Цикл: гейт точности (ai_module) логирует вопрос без ответа → еженедельный дайджест
в админ-чат с кнопками → админ отвечает реплаем → ответ уходит в RAG бессрочно,
кэш инвалидируется, вопрос помечается answered. База растёт от реальных вопросов.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import UnansweredQuestion

logger = logging.getLogger(__name__)

# message_id дайджест-сообщения в админ-чате → id вопроса (для ответа реплаем)
_PENDING_ANSWER_MSGS: dict[int, int] = {}
_PENDING_ANSWER_MSGS_MAX = 200


def _norm_key(question: str) -> str:
    """Нормализует вопрос для группировки повторов (как ключ кэша ответов)."""
    from app.services.ai_module import _normalize_cache_key
    return _normalize_cache_key(question)[:200]


async def log_unanswered(chat_id: int, question: str) -> None:
    """Записывает «не знаю»-вопрос (или инкрементирует повтор). Никогда не бросает."""
    question = question.strip()[:500]
    if len(question) < 5:
        return
    key = _norm_key(question)
    if not key:
        return
    try:
        async for session in get_session():
            existing = await session.scalar(
                select(UnansweredQuestion).where(
                    UnansweredQuestion.norm_key == key,
                    UnansweredQuestion.chat_id == chat_id,
                    UnansweredQuestion.status == "open",
                ).limit(1)
            )
            now = datetime.now(timezone.utc)
            if existing is not None:
                existing.hits += 1
                existing.last_asked_at = now
                existing.question = question  # свежая формулировка
            else:
                session.add(UnansweredQuestion(
                    chat_id=chat_id, question=question, norm_key=key,
                    last_asked_at=now,
                ))
            await session.commit()
            logger.info("UNANSWERED: записан вопрос без ответа: %r", question[:80])
    except Exception:
        logger.warning("UNANSWERED: не удалось записать вопрос.", exc_info=True)


STALE_PREFIX = "[УСТАРЕЛО]"


async def log_stale_report(chat_id: int, question: str, reply: str) -> None:
    """Фиксирует жалобу «данные устарели» в той же таблице безответных вопросов.

    Переиспользуем весь жизненный цикл: запись попадает в /kb_stale и в
    еженедельный дайджест с кнопками «Ответить/Скрыть» — свежий ответ админа
    уйдёт в RAG и закроет жалобу. Никогда не бросает.
    """
    question = question.strip()[:300]
    if len(question) < 5:
        return
    key = _norm_key(question)
    if not key:
        return
    text = f"{STALE_PREFIX} {question}\nОтвет бота был: {reply.strip()[:180]}"
    try:
        async for session in get_session():
            existing = await session.scalar(
                select(UnansweredQuestion).where(
                    UnansweredQuestion.norm_key == key,
                    UnansweredQuestion.chat_id == chat_id,
                    UnansweredQuestion.status == "open",
                    UnansweredQuestion.question.startswith(STALE_PREFIX),
                ).limit(1)
            )
            now = datetime.now(timezone.utc)
            if existing is not None:
                existing.hits += 1
                existing.last_asked_at = now
            else:
                session.add(UnansweredQuestion(
                    chat_id=chat_id, question=text[:500], norm_key=key,
                    last_asked_at=now,
                ))
            await session.commit()
            logger.info("STALE: жалоба на устаревшие данные записана: %r", question[:80])
    except Exception:
        logger.warning("STALE: не удалось записать жалобу.", exc_info=True)


async def list_open_stale_reports() -> list[str]:
    """Открытые жалобы «устарело» для отчёта /kb_stale (свежие первыми)."""
    try:
        async for session in get_session():
            rows = (await session.execute(
                select(UnansweredQuestion)
                .where(
                    UnansweredQuestion.status == "open",
                    UnansweredQuestion.question.startswith(STALE_PREFIX),
                )
                .order_by(UnansweredQuestion.last_asked_at.desc())
                .limit(10)
            )).scalars().all()
            return [
                q.question.splitlines()[0].removeprefix(STALE_PREFIX).strip()
                + (f" (×{q.hits})" if q.hits > 1 else "")
                for q in rows
            ]
    except Exception:
        logger.warning("STALE: не удалось прочитать жалобы.", exc_info=True)
    return []


def _digest_keyboard(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"unq:ans:{question_id}"),
        InlineKeyboardButton(text="🙈 Скрыть", callback_data=f"unq:skip:{question_id}"),
    ]])


async def send_unanswered_digest(bot: Bot, limit: int = 10) -> None:
    """Еженедельный дайджест незакрытых вопросов в админ-чат (job)."""
    try:
        async for session in get_session():
            rows = (await session.execute(
                select(UnansweredQuestion)
                .where(UnansweredQuestion.status == "open")
                .order_by(UnansweredQuestion.hits.desc(), UnansweredQuestion.last_asked_at.desc())
                .limit(limit)
            )).scalars().all()
            break
        else:
            return
        if not rows:
            logger.info("UNANSWERED: дайджест пропущен — открытых вопросов нет.")
            return

        await bot.send_message(
            settings.admin_log_chat_id,
            f"📋 Вопросы жителей без ответа за неделю (топ-{len(rows)}).\n"
            "«Ответить» → пришлите ответ реплаем, он уйдёт в базу знаний.",
        )
        for q in rows:
            hits = f" (спрашивали ×{q.hits})" if q.hits > 1 else ""
            await bot.send_message(
                settings.admin_log_chat_id,
                f"❓ {q.question[:400]}{hits}",
                reply_markup=_digest_keyboard(q.id),
            )
        logger.info("UNANSWERED: дайджест из %d вопросов отправлен.", len(rows))
    except Exception:
        logger.warning("UNANSWERED: не удалось отправить дайджест.", exc_info=True)


def register_pending_answer(message_id: int, question_id: int) -> None:
    """Связывает сообщение «ответьте реплаем» с вопросом."""
    if len(_PENDING_ANSWER_MSGS) >= _PENDING_ANSWER_MSGS_MAX:
        oldest = next(iter(_PENDING_ANSWER_MSGS))
        _PENDING_ANSWER_MSGS.pop(oldest, None)
    _PENDING_ANSWER_MSGS[message_id] = question_id


def pop_pending_answer(message_id: int) -> int | None:
    """Возвращает id вопроса, если message_id — приглашение к ответу."""
    return _PENDING_ANSWER_MSGS.pop(message_id, None)


def peek_pending_answer(message_id: int) -> int | None:
    return _PENDING_ANSWER_MSGS.get(message_id)


async def set_status(session: AsyncSession, question_id: int, status: str) -> UnansweredQuestion | None:
    q = await session.get(UnansweredQuestion, question_id)
    if q is None:
        return None
    q.status = status
    await session.commit()
    return q


async def save_admin_answer(question_id: int, answer_text: str, admin_id: int) -> str | None:
    """Пишет ответ админа в RAG бессрочно, инвалидирует кэш, закрывает вопрос.

    Возвращает текст вопроса при успехе, иначе None.
    """
    answer_text = answer_text.strip()
    if len(answer_text) < 3:
        return None
    try:
        async for session in get_session():
            q = await session.get(UnansweredQuestion, question_id)
            if q is None:
                return None
            from app.services.rag import add_rag_message
            # Для жалоб «устарело» берём только сам вопрос (первую строку без
            # префикса) — старый неверный ответ бота в RAG попадать не должен.
            # Обычные вопросы идут как есть.
            if q.question.startswith(STALE_PREFIX):
                question_text = q.question.splitlines()[0].removeprefix(STALE_PREFIX).strip()
            else:
                question_text = q.question
            fact = f"[Ответ администратора] Вопрос: {question_text[:300]}\nОтвет: {answer_text[:600]}"
            await add_rag_message(
                session,
                chat_id=q.chat_id,
                message_text=fact,
                added_by_user_id=admin_id,
                source_user_id=admin_id,
                is_admin=True,
            )
            q.status = "answered"
            await session.commit()

            # Свежий ответ должен отдаваться сразу — чистим кэш по словам вопроса
            try:
                from app.services.ai_module import invalidate_cache_by_keywords
                words = [w for w in q.norm_key.split("|") if len(w) >= 3][:7]
                invalidate_cache_by_keywords(words)
            except Exception:
                pass
            logger.info("UNANSWERED: вопрос #%d закрыт ответом админа.", question_id)
            return q.question
    except Exception:
        logger.warning("UNANSWERED: не удалось сохранить ответ.", exc_info=True)
    return None
