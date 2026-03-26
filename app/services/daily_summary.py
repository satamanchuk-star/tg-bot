"""Почему: единая сборка ежедневной сводки упрощает контроль качества и приватности."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import MessageLog, ModerationEvent


# Стоп-слова для фильтрации топ-слов (не несут смысловой нагрузки)
_WORD_STOP_LIST = {
    "тоже", "потом", "можно", "нужно", "через", "просто", "только", "очень",
    "всем", "ещё", "будет", "была", "было", "были", "есть", "нету",
    "такой", "такая", "такое", "такие", "этот", "этого", "этой", "этих",
    "свой", "свою", "своё", "своего", "своей", "своих",
    "который", "которая", "которое", "которые", "которого",
    "когда", "тогда", "потому", "поэтому", "чтобы", "если",
    "какой", "какая", "какое", "какие", "какого",
    "ничего", "никто", "может", "могу", "могут",
    "сейчас", "вообще", "кстати", "кажется", "наверное", "вроде",
    "спасибо", "пожалуйста", "привет", "здравствуйте",
    "написал", "написала", "написали", "пишет", "пишут",
    "делать", "делает", "делают", "сделать", "сделал", "сделали",
    "знает", "знаю", "знают", "думаю", "думает",
    "хочу", "хочет", "хотел", "хотела", "хотели",
    "говорит", "говорят", "сказал", "сказала",
    "нормально", "хорошо", "плохо", "ладно",
    "вопрос", "ответ", "тема", "чате", "чата", "форуме",
    "соседи", "соседей", "человек",
}


def _build_topic_name_map() -> dict[int, str]:
    """Строит маппинг topic_id → человекочитаемое название."""
    mapping: dict[int, str] = {}
    topic_data = [
        (settings.topic_gate, "Шлагбаум"),
        (settings.topic_repair, "Ремонт"),
        (settings.topic_complaints, "Жалобы"),
        (settings.topic_pets, "Питомцы"),
        (settings.topic_parents, "Мамы и папы"),
        (settings.topic_realty, "Недвижимость"),
        (settings.topic_rides, "Попутчики"),
        (settings.topic_services, "Услуги"),
        (settings.topic_uk, "УК"),
        (settings.topic_smoke, "Курилка"),
        (settings.topic_market, "Барахолка"),
        (settings.topic_neighbors, "Соседи"),
        (settings.topic_games, "Игры"),
        (settings.topic_rules, "Правила"),
        (settings.topic_important, "Важное"),
    ]
    # Дополнительные топики зданий
    for attr_name in ("topic_buildings_41_42", "topic_building_2", "topic_building_3", "topic_duplex"):
        tid = getattr(settings, attr_name, None)
        if tid is not None:
            label = attr_name.replace("topic_", "").replace("_", " ").title()
            mapping[tid] = label
    for topic_id, name in topic_data:
        if topic_id is not None:
            mapping[topic_id] = name
    return mapping


_TOPIC_NAME_MAP: dict[int, str] | None = None


def _get_topic_name(topic_id: int) -> str:
    """Возвращает человекочитаемое название топика по ID."""
    global _TOPIC_NAME_MAP
    if _TOPIC_NAME_MAP is None:
        _TOPIC_NAME_MAP = _build_topic_name_map()
    return _TOPIC_NAME_MAP.get(topic_id, f"тема #{topic_id}")


@dataclass(slots=True)
class SentimentStats:
    positive: int = 0
    neutral: int = 0
    negative: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.neutral + self.negative

    @property
    def mood_label(self) -> str:
        if self.total == 0:
            return "недостаточно данных"
        neg_ratio = self.negative / self.total
        pos_ratio = self.positive / self.total
        if neg_ratio > 0.3:
            return "напряжённое"
        if pos_ratio > 0.4:
            return "позитивное"
        return "спокойное"

    @property
    def trend_emoji(self) -> str:
        label = self.mood_label
        if label == "позитивное":
            return "😊"
        if label == "напряжённое":
            return "😤"
        return "😐"


@dataclass(slots=True)
class DailySummary:
    messages: int
    active_users: int
    warnings: int
    deletions: int
    strikes: int
    conflicts: int
    topics: list[str]
    mood: str
    positive: str
    top_words: list[str]
    top_tagged_users: list[int]
    sentiment: SentimentStats = field(default_factory=SentimentStats)


async def build_daily_summary(session: AsyncSession, chat_id: int) -> DailySummary:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    msg_count = int(
        await session.scalar(
            select(func.count()).select_from(MessageLog).where(
                and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since)
            )
        )
        or 0
    )
    active_users = int(
        await session.scalar(
            select(func.count(func.distinct(MessageLog.user_id))).where(
                and_(
                    MessageLog.chat_id == chat_id,
                    MessageLog.created_at >= since,
                )
            )
        )
        or 0
    )

    events = (
        await session.execute(
            select(ModerationEvent).where(
                and_(ModerationEvent.chat_id == chat_id, ModerationEvent.created_at >= since)
            )
        )
    ).scalars().all()

    warnings = sum(1 for item in events if item.event_type == "warn")
    deletions = sum(1 for item in events if item.event_type == "delete")
    strikes = sum(1 for item in events if item.event_type == "strike")

    topic_rows = (
        await session.execute(
            select(MessageLog.topic_id, func.count(MessageLog.id))
            .where(and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since))
            .group_by(MessageLog.topic_id)
            .order_by(func.count(MessageLog.id).desc())
            .limit(3)
        )
    ).all()
    # Человекочитаемые названия топиков
    topics = [
        f"{_get_topic_name(row[0])} ({row[1]} сообщ.)"
        for row in topic_rows if row[0] is not None
    ]

    text_rows = (
        await session.execute(
            select(MessageLog.text, MessageLog.user_id, MessageLog.sentiment)
            .where(and_(MessageLog.chat_id == chat_id, MessageLog.created_at >= since))
            .limit(2000)
        )
    ).all()
    word_counter: Counter[str] = Counter()
    tagged_counter: Counter[int] = Counter()
    sentiment_stats = SentimentStats()
    for text, user_id, sentiment in text_rows:
        if isinstance(user_id, int):
            tagged_counter[user_id] += 1
        # Агрегируем sentiment
        if sentiment == "positive":
            sentiment_stats.positive += 1
        elif sentiment == "negative":
            sentiment_stats.negative += 1
        elif sentiment == "neutral":
            sentiment_stats.neutral += 1
        if not text:
            continue
        for word in text.lower().split():
            cleaned = word.strip(".,!?()[]{}\"'`""«»—–-")
            if len(cleaned) < 4:
                continue
            if cleaned.startswith("http"):
                continue
            if cleaned in _WORD_STOP_LIST:
                continue
            word_counter[cleaned] += 1

    conflict_buckets: dict[int, set[int]] = defaultdict(set)
    for item in events:
        if item.severity >= 2:
            hour_key = int(item.created_at.timestamp() // 3600)
            conflict_buckets[hour_key].add(item.user_id)
    conflicts = sum(1 for users in conflict_buckets.values() if len(users) >= 2)

    # Настроение определяем по sentiment, а не только по конфликтам
    mood = sentiment_stats.mood_label
    if conflicts > 0 and mood == "спокойное":
        mood = "напряжённое"

    # Вариативный позитивный текст
    positive_text = _pick_positive_comment(summary_stats={
        "messages": msg_count,
        "active_users": active_users,
        "mood": mood,
        "conflicts": conflicts,
        "sentiment": sentiment_stats,
    })

    return DailySummary(
        messages=msg_count,
        active_users=active_users,
        warnings=warnings,
        deletions=deletions,
        strikes=strikes,
        conflicts=conflicts,
        topics=topics,
        mood=mood,
        positive=positive_text,
        top_words=[word for word, _ in word_counter.most_common(8)],
        top_tagged_users=[uid for uid, _ in tagged_counter.most_common(5)],
        sentiment=sentiment_stats,
    )


def _pick_positive_comment(summary_stats: dict) -> str:
    """Выбирает контекстный комментарий на основе реальных данных дня."""
    mood = summary_stats.get("mood", "спокойное")
    messages = summary_stats.get("messages", 0)
    active_users = summary_stats.get("active_users", 0)
    conflicts = summary_stats.get("conflicts", 0)
    sentiment: SentimentStats | None = summary_stats.get("sentiment")

    if conflicts >= 3:
        return random.choice([
            "Горячий день: несколько конфликтных ситуаций, стоит обратить внимание.",
            "День был напряжённым — много споров. Возможно, стоит напомнить о правилах общения.",
            "Были конфликты в нескольких темах, ситуация требует мониторинга.",
        ])
    if conflicts == 1 or conflicts == 2:
        return random.choice([
            "Было пару горячих моментов, но в целом всё под контролем.",
            "Небольшие трения в паре обсуждений, но без серьёзных последствий.",
            "Пара конфликтных ситуаций, остальные темы шли спокойно.",
        ])
    if mood == "позитивное":
        return random.choice([
            "Настроение в чате отличное — соседи помогали друг другу и общались дружелюбно.",
            "Позитивный день: много полезных обсуждений и взаимопомощи.",
            "Хороший день — общение было дружелюбным и конструктивным.",
        ])
    if messages == 0:
        return "Тихий день — сообщений не было."
    if messages < 20:
        return random.choice([
            "Тихий день, мало активности в чате.",
            "Спокойный день с небольшим количеством сообщений.",
        ])
    if active_users > 15:
        return random.choice([
            "Активный день — много соседей участвовало в обсуждениях.",
            f"В обсуждениях участвовало {active_users} человек — хорошая вовлечённость.",
        ])
    return random.choice([
        "День прошёл ровно и спокойно.",
        "Обычный рабочий день без происшествий.",
        "Штатный день, обсуждения шли в спокойном русле.",
        "Ничего необычного — стандартный день в чате.",
    ])


def build_ai_summary_context(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "нет выделенных тем"
    words = ", ".join(summary.top_words) if summary.top_words else "недостаточно данных"
    tagged = ", ".join(str(uid) for uid in summary.top_tagged_users) if summary.top_tagged_users else "н/д"
    sentiment_line = (
        f"позитивных={summary.sentiment.positive}, "
        f"нейтральных={summary.sentiment.neutral}, "
        f"негативных={summary.sentiment.negative}"
    )
    return (
        "Контекст за последние 24 часа:\n"
        f"- Сообщений: {summary.messages}\n"
        f"- Активных пользователей: {summary.active_users}\n"
        f"- Предупреждений: {summary.warnings}\n"
        f"- Удалений: {summary.deletions}\n"
        f"- Страйков: {summary.strikes}\n"
        f"- Конфликтных часов: {summary.conflicts}\n"
        f"- Тональность сообщений: {sentiment_line}\n"
        f"- Основные темы: {topics}\n"
        f"- Топ обсуждаемых слов: {words}\n"
        f"- Самые активные пользователи (id): {tagged}\n"
        "Сформируй короткое резюме для админов (до 600 символов), включая:\n"
        "- Оценку настроения чата\n"
        "- Ключевые темы дня\n"
        "- Рекомендации, если были конфликты или аномально мало/много активности"
    )


_RESPONSE_SOURCE_LABELS: dict[str, str] = {
    "resident_kb": "База знаний ЖК (прямой ответ)",
    "resident_kb_context_ai": "База знаний ЖК + ИИ",
    "rag_ai": "RAG + ИИ",
    "faq_ai": "FAQ + ИИ",
    "places_ai": "Инфраструктура + ИИ",
    "services_ai": "Услуги жителей + ИИ",
    "web_ai": "Веб-поиск + ИИ",
    "fallback_ai": "ИИ без контекста",
    "rag": "RAG (локально)",
    "faq": "FAQ (локально)",
    "places": "Инфраструктура (локально)",
    "services": "Услуги (локально)",
    "web": "Веб-поиск (локально)",
    "rule": "Тематическое правило",
    "fallback": "Не знаю (fallback)",
    "resident_kb_direct": "База знаний ЖК",
    "greeting": "Приветствие",
    "thanks": "Благодарность",
    "empty_prompt": "Пустой запрос",
    "forbidden_topic": "Запрещённая тема",
    "mention_random": "Упоминание (случайная шутка)",
    "mention_no_prompt": "Упоминание без вопроса",
    "mention_ai_error": "Упоминание → ошибка ИИ",
    "ai_error_local_fallback": "Ошибка ИИ → локальный fallback",
}


def build_response_report(response_log: list[dict]) -> str:
    """Формирует текст ежедневного отчёта по логике ответов бота."""
    if not response_log:
        return "Отчёт по ответам бота: за сутки ни одного ответа не зафиксировано."

    from collections import Counter
    total = len(response_log)
    ai_count = sum(1 for r in response_log if r.get("used_ai"))
    source_counter: Counter[str] = Counter(r["source"] for r in response_log)

    lines = [
        f"Отчёт по логике ответов бота за сутки:",
        f"• Всего ответов: {total} (из них через ИИ: {ai_count}, локально: {total - ai_count})",
        "",
        "Разбивка по источникам:",
    ]

    for source, count in source_counter.most_common():
        label = _RESPONSE_SOURCE_LABELS.get(source, source)
        pct = round(count / total * 100)
        lines.append(f"  — {label}: {count} ({pct}%)")

    # Примеры запросов по ключевым источникам
    examples: dict[str, list[str]] = {}
    for r in response_log:
        src = r["source"]
        p = r.get("prompt", "").strip()
        if p and src not in ("greeting", "thanks", "empty_prompt", "mention_no_prompt", "mention_random"):
            examples.setdefault(src, [])
            if len(examples[src]) < 2:
                examples[src].append(p[:60])

    if examples:
        lines.append("")
        lines.append("Примеры вопросов по источникам:")
        for src, prompts in list(examples.items())[:6]:
            label = _RESPONSE_SOURCE_LABELS.get(src, src)
            for p in prompts:
                lines.append(f"  [{label}] «{p}»")

    # Предупреждение о частом fallback
    fallback_sources = {"fallback", "fallback_ai", "ai_error_local_fallback", "mention_ai_error"}
    fallback_count = sum(source_counter.get(s, 0) for s in fallback_sources)
    if fallback_count > 0 and total > 0:
        fallback_pct = round(fallback_count / total * 100)
        if fallback_pct >= 30:
            lines.append("")
            lines.append(
                f"⚠️ Внимание: {fallback_pct}% ответов — fallback (бот не нашёл данных). "
                "Рекомендую пополнить базу знаний или RAG."
            )
        elif fallback_pct >= 15:
            lines.append("")
            lines.append(
                f"ℹ️ Fallback составил {fallback_pct}% ответов — база знаний может быть неполной."
            )

    return "\n".join(lines)


def render_daily_summary(summary: DailySummary) -> str:
    topics = ", ".join(summary.topics) if summary.topics else "темы не выделились"
    words_line = ", ".join(summary.top_words[:6]) if summary.top_words else "—"

    # Sentiment-строка
    s = summary.sentiment
    sentiment_line = ""
    if s.total > 0:
        sentiment_line = (
            f"\n• Настроение чата: {s.mood_label} {s.trend_emoji} "
            f"(+{s.positive} / ~{s.neutral} / -{s.negative})"
        )

    # Модерация: показываем только если были события
    mod_parts = []
    if summary.warnings:
        mod_parts.append(f"предупреждений: {summary.warnings}")
    if summary.deletions:
        mod_parts.append(f"удалений: {summary.deletions}")
    if summary.strikes:
        mod_parts.append(f"страйков: {summary.strikes}")
    mod_line = f"\n• Модерация: {', '.join(mod_parts)}" if mod_parts else ""

    return (
        "📊 Статистика за день:\n"
        f"• Сообщений: {summary.messages}\n"
        f"• Активных соседей: {summary.active_users}"
        f"{mod_line}\n"
        f"• Обсуждали: {topics}\n"
        f"• Топ слов: {words_line}"
        f"{sentiment_line}\n"
        f"• {summary.positive}"
    )
