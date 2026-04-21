"""Почему: мягкие еженедельные DM-косания по фактам из профиля повышают вовлечённость.

Бот раз в неделю отбирает жителей, у которых уже сохранены релевантные факты
(интересы, питомцы, машина, дети), и присылает в личку короткое напоминание
вида «вы интересовались X — спросите, если что». Это не реклама и не спам:
- off-by-default через feature flag,
- ограничено N жителями за запуск (по умолчанию 20),
- не чаще одного DM в 30 дней одному человеку,
- безболезненный опт-аут командой /off_nudges,
- если Telegram запрещает DM (житель не писал боту в личку) — помечаем
  профиль unreachable и больше не пытаемся.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import ResidentProfile
from app.services.resident_kb import search_resident_kb

logger = logging.getLogger(__name__)


# Поля профиля, по которым мы способны сформулировать осмысленный нажъм.
# Порядок важен — первый найденный определяет тему сообщения.
_FACT_PRIORITIES = ("interests", "pets", "family", "car")

# Минимальный score из KB, чтобы добавить teaser-строчку из базы знаний.
# Ниже порога — отправляем нажъм без teaser'а.
_KB_TEASER_SCORE = 0.5


@dataclass(slots=True)
class NudgeCandidate:
    user_id: int
    chat_id: int
    display_name: str | None
    facts: dict
    last_nudge_at: datetime | None


def _parse_facts(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_opted_out(facts: dict) -> bool:
    return bool(facts.get("nudge_opt_out") or facts.get("nudge_unreachable"))


def _first_actionable_fact(facts: dict) -> tuple[str, str] | None:
    """Возвращает (label, value) или None, если ни один факт не годится для нажъса."""

    for key in _FACT_PRIORITIES:
        value = facts.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            non_empty = [str(v).strip() for v in value if str(v).strip()]
            if not non_empty:
                continue
            return key, non_empty[0]
        text_value = str(value).strip()
        if not text_value:
            continue
        # family — нажъм имеет смысл только если упомянут ребёнок/дети.
        if key == "family" and not any(
            w in text_value.lower() for w in ("ребён", "ребен", "дети", "малыш", "школьник", "садик")
        ):
            continue
        return key, text_value
    return None


def _kb_teaser(query: str) -> str:
    """Возвращает короткую первую содержательную строку из лучшего ответа KB.

    Пустая строка — KB ничего уверенного не нашла; нажъм отправим без teaser'а.
    """

    try:
        result = search_resident_kb(query, top_k=1)
    except Exception:  # noqa: BLE001
        logger.debug("KB search failed для teaser'а нажъса.", exc_info=True)
        return ""
    if not result.matches:
        return ""
    best = result.matches[0]
    if best.score < _KB_TEASER_SCORE:
        return ""
    for raw_line in best.entry.answer.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Пропускаем строки-заголовки/буллеты/контактные иконки — нужен живой текст.
        if line[0] in {"📞", "🌐", "•", "—", "-", "*", "#", "▪", "▫"}:
            continue
        if len(line) < 12:
            continue
        return line[:200]
    return ""


def build_nudge_message(facts: dict, *, display_name: str | None = None) -> str | None:
    """Собирает текст еженедельного нажъса. None — фактов недостаточно."""

    if _is_opted_out(facts):
        return None
    fact = _first_actionable_fact(facts)
    if fact is None:
        return None
    _, value = fact

    # Имя берём только до первого пробела (Иван Иванов → Иван), чтобы
    # обращение не выглядело формально-фамильным.
    name_part = ""
    if display_name:
        first = display_name.strip().split()[0] if display_name.strip() else ""
        if first and not first.startswith("@"):
            name_part = f", {first}"

    intros = (
        "Привет{name}! Помню, вам близка тема «{topic}».",
        "Привет{name}! Вы как-то спрашивали про «{topic}».",
        "Привет{name}! Думал тут — кажется, вам интересна тема «{topic}».",
    )
    intro = random.choice(intros).format(name=name_part, topic=value[:60])

    teaser = _kb_teaser(value)
    teaser_block = f"\n\nКстати: {teaser}" if teaser else ""

    outro = (
        "\n\nЕсли захотите подробнее — спросите меня в группе через @-упоминание "
        "или прямо здесь, в личке. Чтобы такие сообщения не приходили — пришлите /off_nudges."
    )

    return (intro + teaser_block + outro)[:1000]


async def select_nudge_candidates(
    session: AsyncSession,
    *,
    chat_id: int,
    limit: int,
    min_days_between: int,
) -> list[NudgeCandidate]:
    """Отбирает жителей для рассылки на этой неделе.

    Сортировка: сначала те, кому вообще не писали (last_nudge_at IS NULL),
    затем по возрастанию last_nudge_at. Это даёт честный round-robin.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_days_between)
    stmt = (
        select(ResidentProfile)
        .where(ResidentProfile.chat_id == chat_id)
        .order_by(ResidentProfile.last_nudge_at.asc().nulls_first(), ResidentProfile.user_id)
    )
    rows = (await session.execute(stmt)).scalars().all()

    selected: list[NudgeCandidate] = []
    for row in rows:
        last_nudge = row.last_nudge_at
        # SQLite возвращает datetime без tzinfo — нормализуем для сравнения с cutoff.
        if last_nudge is not None:
            if last_nudge.tzinfo is None:
                last_nudge = last_nudge.replace(tzinfo=timezone.utc)
            if last_nudge > cutoff:
                continue
        facts = _parse_facts(row.facts_json)
        if _is_opted_out(facts):
            continue
        if _first_actionable_fact(facts) is None:
            continue
        selected.append(NudgeCandidate(
            user_id=row.user_id,
            chat_id=row.chat_id,
            display_name=row.display_name,
            facts=facts,
            last_nudge_at=row.last_nudge_at,
        ))
        if len(selected) >= limit:
            break
    return selected


async def _mark_nudge_attempt(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    unreachable: bool = False,
) -> None:
    """Обновляет last_nudge_at и, при необходимости, помечает профиль как unreachable."""

    profile = await session.get(ResidentProfile, {"user_id": user_id, "chat_id": chat_id})
    if profile is None:
        return
    profile.last_nudge_at = datetime.now(timezone.utc)
    if unreachable:
        facts = _parse_facts(profile.facts_json)
        facts["nudge_unreachable"] = True
        profile.facts_json = json.dumps(facts, ensure_ascii=False)
    await session.commit()


async def send_weekly_nudges(bot: Bot) -> None:
    """Точка входа для APScheduler. Безопасна при отключённом feature flag."""

    if not settings.ai_feature_weekly_nudge:
        logger.info("WEEKLY_NUDGE: пропуск — ai_feature_weekly_nudge=false.")
        return

    limit = max(1, int(settings.weekly_nudge_max_per_run))
    min_days = max(1, int(settings.weekly_nudge_min_days_between))

    sent = 0
    forbidden = 0
    skipped = 0

    async for session in get_session():
        candidates = await select_nudge_candidates(
            session,
            chat_id=settings.forum_chat_id,
            limit=limit,
            min_days_between=min_days,
        )
        if not candidates:
            logger.info("WEEKLY_NUDGE: подходящих кандидатов нет.")
            break

        logger.info("WEEKLY_NUDGE: отобрано %d кандидатов (limit=%d).", len(candidates), limit)

        for cand in candidates:
            text = build_nudge_message(cand.facts, display_name=cand.display_name)
            if text is None:
                skipped += 1
                continue
            try:
                await bot.send_message(cand.user_id, text)
                await _mark_nudge_attempt(
                    session, user_id=cand.user_id, chat_id=cand.chat_id,
                )
                sent += 1
                logger.info("WEEKLY_NUDGE: sent user_id=%s", cand.user_id)
            except TelegramForbiddenError:
                # Житель ни разу не писал боту в личку — Telegram не даёт инициировать диалог.
                # Помечаем профиль, чтобы не тратить квоту в следующий раз.
                forbidden += 1
                await _mark_nudge_attempt(
                    session, user_id=cand.user_id, chat_id=cand.chat_id, unreachable=True,
                )
                logger.info(
                    "WEEKLY_NUDGE: TelegramForbidden user_id=%s — помечен unreachable.",
                    cand.user_id,
                )
            except TelegramRetryAfter as exc:
                # Telegram попросил подождать — прерываем рассылку, продолжим в следующий запуск.
                logger.warning(
                    "WEEKLY_NUDGE: rate limited (retry after %ss), останавливаемся.",
                    getattr(exc, "retry_after", "?"),
                )
                break
            except Exception:  # noqa: BLE001
                # Любая другая ошибка — не дожимаем профиль, чтобы попробовать в следующий раз.
                logger.exception(
                    "WEEKLY_NUDGE: ошибка отправки user_id=%s — не помечаем профиль.",
                    cand.user_id,
                )
        break

    logger.info(
        "WEEKLY_NUDGE: завершено. sent=%d forbidden=%d skipped=%d",
        sent, forbidden, skipped,
    )
