"""Почему: без цифр настройка бота — гадание.

Владелец видел только расходы на AI: сколько людей реально играет, справедливо
ли матчер отвергает ответы, куда уходят монеты — было не видно. Здесь один
сборщик отчёта на все окна: команда `/аналитика` (по умолчанию 7 дней) и
вечерняя сводка (за сегодня) зовут одну и ту же функцию.

Все запросы — только чтение и агрегаты; отчёт не должен ронять бот, поэтому
каждый блок обёрнут и при ошибке молча пропускается.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiFeedback,
    AiTaskLog,
    GameRound,
    MessageLog,
    Place,
    QuizAnswerMiss,
    QuizQuestion,
    QuizRound,
    UnansweredQuestion,
    UserStat,
)

logger = logging.getLogger(__name__)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение числительных: 1 житель, 2 жителя, 5 жителей."""
    if 11 <= n % 100 <= 14:
        return many
    tail = n % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _topic_name(topic_id: int | None) -> str:
    """Читаемое имя темы: сначала карта подсказок ассистента, затем свой
    справочник (в карте нет тем, где бот не отвечает, — например «Игры»)."""
    if topic_id is None:
        return "General"
    from app.config import settings
    from app.services.ai_module import get_topic_name

    known = {
        settings.topic_games: "Игры",
        settings.topic_rides: "Попутчики",
        settings.topic_market: "Барахолка",
        settings.topic_ads: "Объявления",
        settings.topic_neighbors: "Соседи",
        settings.topic_rules: "Правила",
        settings.topic_important: "Важное",
        settings.topic_services: "Услуги",
        settings.topic_smoke: "Курение",
        settings.topic_duplex: "Дуплексы",
    }
    return get_topic_name(topic_id) or known.get(topic_id) or f"тема {topic_id}"


async def _activity_block(session: AsyncSession, since: datetime) -> list[str]:
    """Активность жителей: сообщения, уникальные авторы, самые живые темы."""
    msgs, people = (await session.execute(
        select(func.count(MessageLog.id), func.count(func.distinct(MessageLog.user_id)))
        .where(MessageLog.created_at >= since)
    )).one()
    if not msgs:
        return ["👥 Жители: тишина в чате за период."]
    lines = [
        f"👥 Жители: {msgs} сообщений от {people} "
        f"{_plural(people, 'человека', 'человек', 'человек')}"
    ]
    top = (await session.execute(
        select(MessageLog.topic_id, func.count(MessageLog.id))
        .where(MessageLog.created_at >= since, MessageLog.topic_id.is_not(None))
        .group_by(MessageLog.topic_id)
        .order_by(func.count(MessageLog.id).desc())
        .limit(3)
    )).all()
    if top:
        parts = [f"{_topic_name(tid)} ×{n}" for tid, n in top]
        lines.append(f"   Живее всего: {', '.join(parts)}")
    return lines


async def _ai_block(session: AsyncSession, since: datetime) -> list[str]:
    """Работа ассистента: объём, стоимость, оценки жителей, «не знаю»."""
    requests_n, tokens_n, cost = (await session.execute(
        select(
            func.count(AiTaskLog.id),
            func.coalesce(func.sum(AiTaskLog.tokens_used), 0),
            func.coalesce(func.sum(AiTaskLog.cost_usd), 0.0),
        ).where(AiTaskLog.created_at >= since)
    )).one()
    lines = [
        f"🤖 Ассистент: {requests_n} запросов · {int(tokens_n):,} токенов"
        .replace(",", " ") + (f" · ≈${float(cost):.2f}" if cost else "")
    ]
    by_task = (await session.execute(
        select(AiTaskLog.task, func.count(AiTaskLog.id))
        .where(AiTaskLog.created_at >= since)
        .group_by(AiTaskLog.task)
        .order_by(func.count(AiTaskLog.id).desc())
        .limit(4)
    )).all()
    if by_task:
        lines.append("   Задачи: " + ", ".join(f"{t} ×{n}" for t, n in by_task))

    up = int(await session.scalar(
        select(func.count(AiFeedback.id))
        .where(AiFeedback.created_at >= since, AiFeedback.rating > 0)
    ) or 0)
    down = int(await session.scalar(
        select(func.count(AiFeedback.id))
        .where(AiFeedback.created_at >= since, AiFeedback.rating < 0)
    ) or 0)
    if up or down:
        lines.append(f"   Оценки жителей: 👍 {up} · 👎 {down}")

    open_q = int(await session.scalar(
        select(func.count(UnansweredQuestion.id))
        .where(UnansweredQuestion.status == "open")
    ) or 0)
    if open_q:
        lines.append(f"   Вопросов без ответа: {open_q} (дайджест — по понедельникам)")
    return lines


async def _quiz_block(session: AsyncSession, since: datetime) -> list[str]:
    """Викторина: участие, монеты и — главное — справедливость матчера."""
    rounds_n, players_n, correct_n, coins_n = (await session.execute(
        select(
            func.count(QuizRound.id),
            func.count(func.distinct(QuizRound.user_id)),
            func.coalesce(func.sum(QuizRound.correct_answers), 0),
            func.coalesce(func.sum(QuizRound.coins_awarded), 0),
        ).where(QuizRound.finished_at >= since)
    )).one()

    fresh = int(await session.scalar(
        select(func.count(QuizQuestion.id)).where(QuizQuestion.used_at.is_(None))
    ) or 0)

    if not rounds_n:
        return [f"🧠 Викторина: туров не было. Свежих вопросов в банке: {fresh}."]

    lines = [
        f"🧠 Викторина: {players_n} "
        f"{_plural(players_n, 'участник', 'участника', 'участников')} · "
        f"{int(correct_n)} верных ответов · {int(coins_n)} 🪙 выдано"
    ]
    # Банк не бесконечен: сообщаем, на сколько вечеров осталось.
    from app.services.quiz import QUESTIONS_PER_ROUND
    evenings = fresh // max(QUESTIONS_PER_ROUND, 1)
    lines.append(
        f"   Банк: {fresh} {_plural(fresh, 'вопрос', 'вопроса', 'вопросов')} ≈ на "
        f"{evenings} {_plural(evenings, 'вечер', 'вечера', 'вечеров')}"
    )

    near_n = int(await session.scalar(
        select(func.count(QuizAnswerMiss.id))
        .where(QuizAnswerMiss.created_at >= since, QuizAnswerMiss.verdict == "near")
    ) or 0)
    wrong_n = int(await session.scalar(
        select(func.count(QuizAnswerMiss.id))
        .where(QuizAnswerMiss.created_at >= since, QuizAnswerMiss.verdict == "wrong")
    ) or 0)
    if near_n or wrong_n:
        lines.append(f"   Не засчитано: {near_n} «почти» + {wrong_n} мимо")
    # «Почти»-промахи — кандидаты на послабление матчера: если среди них есть
    # честно верные ответы, значит правила всё ещё придирчивы.
    examples = (await session.execute(
        select(QuizAnswerMiss.correct_answer, QuizAnswerMiss.given_text)
        .where(QuizAnswerMiss.created_at >= since, QuizAnswerMiss.verdict == "near")
        .order_by(QuizAnswerMiss.created_at.desc())
        .limit(5)
    )).all()
    for correct, given in examples:
        lines.append(f"      «{correct[:40]}» ← писали «{given[:40]}»")
    return lines


async def _blackjack_block(session: AsyncSession, since: datetime) -> list[str]:
    """Блэкджек и экономика монет: не разгоняется ли инфляция."""
    games_n, bets, payouts = (await session.execute(
        select(
            func.count(GameRound.id),
            func.coalesce(func.sum(GameRound.bet), 0),
            func.coalesce(func.sum(GameRound.payout), 0),
        ).where(GameRound.finished_at >= since)
    )).one()
    total_coins = int(await session.scalar(
        select(func.coalesce(func.sum(UserStat.coins), 0))
    ) or 0)
    holders = int(await session.scalar(select(func.count(UserStat.user_id))) or 0)
    lines: list[str] = []
    if games_n:
        delta = int(payouts) - int(bets)
        lines.append(
            f"🃏 «21»: {games_n} партий · ставок {int(bets)} 🪙 · "
            f"выплат {int(payouts)} 🪙 (казна {delta:+d})"
        )
    if holders:
        lines.append(f"💰 В обороте: {total_coins} 🪙 у {holders} жителей")
    return lines


async def _data_block(session: AsyncSession) -> list[str]:
    """Свежесть справочных данных — то, чем бот отвечает жителям."""
    places_n = int(await session.scalar(
        select(func.count(Place.id)).where(Place.is_active.is_(True))
    ) or 0)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    stale_n = int(await session.scalar(
        select(func.count(Place.id)).where(
            Place.is_active.is_(True),
            (Place.verified_at.is_(None)) | (Place.verified_at < stale_cutoff),
        )
    ) or 0)
    from app.services.resident_kb import load_resident_kb
    kb_n = len(load_resident_kb())
    line = (
        f"📚 База: {places_n} {_plural(places_n, 'место', 'места', 'мест')} + "
        f"{kb_n} {_plural(kb_n, 'запись', 'записи', 'записей')} знаний"
    )
    if stale_n:
        line += f" · несвежих мест: {stale_n} (/kb_stale)"
    return [line]


async def build_analytics_report(session: AsyncSession, *, days: int = 7) -> str:
    """Собирает текстовый отчёт за последние `days` суток."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    header = (
        f"📊 Аналитика за {days} "
        f"{_plural(days, 'день', 'дня', 'дней')}"
    )
    lines = [header, "━━━━━━━━━━━━"]
    blocks = (
        ("активность", _activity_block(session, since)),
        ("ассистент", _ai_block(session, since)),
        ("викторина", _quiz_block(session, since)),
        ("блэкджек", _blackjack_block(session, since)),
        ("данные", _data_block(session)),
    )
    for name, coro in blocks:
        try:
            lines.extend(await coro)
        except Exception:  # noqa: BLE001 — один битый блок не должен ронять отчёт
            logger.warning("ANALYTICS: блок %s не собрался.", name, exc_info=True)
            lines.append(f"⚠️ Блок «{name}» не собрался — см. логи.")
    return "\n".join(lines)
