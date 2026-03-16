"""RAG-сервис: накопление, систематизация и поиск знаний чата ЖК.

Почему: админы добавляют сырые сообщения, а бот хранит и объединяет их
в компактные смысловые блоки для более точных ответов.

TF-IDF ранжирование обеспечивает семантически точный поиск без внешних зависимостей.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RagMessage

_STOP_WORDS = {
    "это",
    "как",
    "что",
    "когда",
    "где",
    "или",
    "для",
    "если",
    "чтобы",
    "можно",
    "нужно",
    "через",
    "просто",
    "только",
    "очень",
    "пожалуйста",
    "всем",
    "тут",
    "там",
    "про",
    "под",
    "над",
    "без",
    "еще",
    "уже",
    "тоже",
    "потом",
    "сейчас",
    "будет",
    "была",
    "было",
    "были",
    "есть",
    "нет",
}
_NORMALIZED_STOP_WORDS = {word.replace("ё", "е") for word in _STOP_WORDS}

_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("транспорт", ("метро", "автобус", "маршрут", "электричк", "остановк", "станци", "доехат", "добрат", "пешком", "транспорт")),
    ("парковка", ("парков", "шлагбаум", "машин", "мест", "паркинг")),
    ("безопасность_и_доступ", ("домофон", "код", "доступ", "замок", "сигнализац", "подъезд")),
    ("лифт", ("лифт", "этаж")),
    ("ук", ("ук", "управля", "диспетчер", "заявк")),
    ("коммуналка", ("вода", "свет", "отоплен", "счетчик")),
    ("безопасность", ("охран", "камера", "пропуск", "консьерж")),
    ("детская_площадка", ("площадк", "качел", "горк", "песочниц", "дет")),
    ("коммунальные_сервисы", ("электр", "сантех", "канализац", "мусор", "дворник")),
    ("платежи", ("квитанц", "оплат", "тариф", "долг", "начислен")),
]

# Дефолтный TTL для RAG-записей (90 дней)
_DEFAULT_RAG_TTL_DAYS = 90
# Коэффициент затухания по времени (чем старше запись, тем ниже score)
_TIME_DECAY_HALF_LIFE_DAYS = 60


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[а-яёa-z0-9]+", text.lower()) if len(w) >= 3]


def _normalize_token(token: str) -> str:
    return token.replace("ё", "е")


def _content_tokens(text: str) -> list[str]:
    """Токены без стоп-слов — для TF-IDF."""
    normalized_tokens = [_normalize_token(word) for word in _tokenize(text)]
    return [word for word in normalized_tokens if word not in _NORMALIZED_STOP_WORDS]


def _bounded_levenshtein(first: str, second: str, max_distance: int = 1) -> int:
    if first == second:
        return 0
    if abs(len(first) - len(second)) > max_distance:
        return max_distance + 1

    previous = list(range(len(second) + 1))
    for i, char_first in enumerate(first, 1):
        current = [i]
        min_row = i
        for j, char_second in enumerate(second, 1):
            cost = 0 if char_first == char_second else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
            min_row = min(min_row, current[-1])
        if min_row > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _common_prefix_len(first: str, second: str) -> int:
    max_len = min(len(first), len(second))
    idx = 0
    while idx < max_len and first[idx] == second[idx]:
        idx += 1
    return idx


def _token_similarity(first: str, second: str) -> float:
    """Оценивает близость токенов: exact > typo > common prefix."""
    if first == second:
        return 1.0

    if len(first) >= 5 and len(second) >= 5:
        if _bounded_levenshtein(first, second, max_distance=1) <= 1:
            return 0.92

        prefix_len = _common_prefix_len(first, second)
        if prefix_len >= 5:
            shorter = min(len(first), len(second))
            return 0.65 + 0.3 * (prefix_len / shorter)

    return 0.0


def _semantic_overlap_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    """Мягкий overlap: учитывает словоформы и мелкие опечатки между query/doc."""
    if not query_tokens or not doc_tokens:
        return 0.0

    doc_terms = set(doc_tokens)
    matched = 0.0
    for query_token in set(query_tokens):
        best = 0.0
        for doc_term in doc_terms:
            similarity = _token_similarity(query_token, doc_term)
            if similarity > best:
                best = similarity
                if best == 1.0:
                    break
        matched += best

    return matched / max(len(set(query_tokens)), 1)


def _token_set(text: str) -> set[str]:
    return set(_tokenize(text))


def _normalize_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    return compact[:1500]


def classify_rag_message(text: str) -> str:
    tokens = _token_set(text)
    for category, markers in _CATEGORY_RULES:
        if any(any(token.startswith(marker) for token in tokens) for marker in markers):
            return category
    return "общее"


def build_semantic_key(text: str, category: str) -> str:
    tokens = [token for token in _tokenize(text) if token not in _STOP_WORDS]
    if not tokens:
        return f"{category}:пусто"
    uniq = sorted(set(tokens), key=lambda item: (-tokens.count(item), item))
    core = uniq[:4]
    return f"{category}:{'|'.join(core)}"[:120]


def build_canonical_text(texts: list[str]) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        normalized = _normalize_text(raw)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    if not ordered:
        return ""
    if len(ordered) == 1:
        return ordered[0]
    merged = "; ".join(ordered[:3])
    if len(ordered) > 3:
        merged += "; ..."
    return merged[:1500]


# ---------------------------------------------------------------------------
# TF-IDF ранжирование (без внешних зависимостей)
# ---------------------------------------------------------------------------

class _TfIdfRanker:
    """Лёгкий TF-IDF ранкер: строится по корпусу RAG-документов."""

    def __init__(self, documents: list[list[str]]) -> None:
        self._n_docs = max(len(documents), 1)
        self._df: Counter[str] = Counter()
        for doc in documents:
            for term in set(doc):
                self._df[term] += 1

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log((self._n_docs + 1) / (df + 1)) + 1.0

    def score(self, doc_tokens: list[str], query_tokens: list[str]) -> float:
        if not doc_tokens or not query_tokens:
            return 0.0
        doc_tf = Counter(doc_tokens)
        doc_len = len(doc_tokens)
        total = 0.0
        for term in set(query_tokens):
            tf = doc_tf.get(term, 0) / doc_len
            total += tf * self._idf(term)
        return total


def _time_decay_factor(created_at: datetime | None) -> float:
    """Экспоненциальное затухание: 1.0 для свежих, ~0.5 через half_life дней."""
    if created_at is None:
        return 0.5
    age_days = (datetime.utcnow() - created_at).total_seconds() / 86400
    if age_days <= 0:
        return 1.0
    return math.exp(-0.693 * age_days / _TIME_DECAY_HALF_LIFE_DAYS)


async def add_rag_message(
    session: AsyncSession,
    *,
    chat_id: int,
    message_text: str,
    added_by_user_id: int,
    source_user_id: int | None = None,
    source_message_id: int | None = None,
    ttl_days: int | None = None,
    is_admin: bool = False,
) -> RagMessage:
    """Добавляет сообщение в RAG-базу и сразу классифицирует его."""
    cleaned = _normalize_text(message_text)
    category = classify_rag_message(cleaned)
    semantic_key = build_semantic_key(cleaned, category)
    now = datetime.utcnow()
    expires_at = now + timedelta(days=ttl_days or _DEFAULT_RAG_TTL_DAYS)
    record = RagMessage(
        chat_id=chat_id,
        message_text=cleaned,
        added_by_user_id=added_by_user_id,
        source_user_id=source_user_id,
        source_message_id=source_message_id,
        is_admin=is_admin,
        rag_category=category,
        rag_semantic_key=semantic_key,
        rag_canonical_text=cleaned,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(record)
    await session.flush()
    return record


async def get_all_rag_messages(session: AsyncSession, chat_id: int) -> list[RagMessage]:
    """Возвращает актуальные (не истёкшие) сообщения RAG для чата."""
    now = datetime.utcnow()
    result = await session.execute(
        select(RagMessage)
        .where(
            and_(
                RagMessage.chat_id == chat_id,
                # Показываем записи без expires_at (старые) или ещё не истёкшие
                (RagMessage.expires_at.is_(None)) | (RagMessage.expires_at > now),
            )
        )
        .order_by(RagMessage.created_at.asc())
    )
    return list(result.scalars().all())


def rank_rag_messages(
    messages: list[RagMessage],
    *,
    query: str,
    top_k: int | None = None,
) -> list[RagMessage]:
    """Ранжирует RAG-сообщения по TF-IDF релевантности и приоритету админских записей."""
    if not messages:
        return []

    query_tokens = _content_tokens(query)
    if not query_tokens:
        ranked_no_query = sorted(
            messages,
            key=lambda msg: (not bool(msg.is_admin), msg.created_at or datetime.min),
            reverse=False,
        )
        return ranked_no_query[:top_k] if top_k is not None else ranked_no_query

    # Строим корпус для IDF
    doc_tokens_list: list[list[str]] = []
    for msg in messages:
        text = msg.rag_canonical_text or msg.message_text
        doc_tokens_list.append(_content_tokens(text))

    ranker = _TfIdfRanker(doc_tokens_list)

    scored: list[tuple[RagMessage, float]] = []
    for msg, doc_tokens in zip(messages, doc_tokens_list):
        tfidf_score = ranker.score(doc_tokens, query_tokens)
        overlap_score = _semantic_overlap_score(query_tokens, doc_tokens)
        decay = _time_decay_factor(msg.created_at)
        # Итоговый score: tfidf + мягкое совпадение токенов, затем time_decay
        lexical_score = 0.75 * tfidf_score + 0.25 * overlap_score
        final_score = lexical_score * (0.7 + 0.3 * decay)
        scored.append((msg, final_score))

    scored.sort(
        key=lambda item: (
            not bool(item[0].is_admin),
            -item[1],
            -(item[0].created_at.timestamp() if item[0].created_at else 0),
        )
    )
    ranked = [msg for msg, _score in scored]
    return ranked[:top_k] if top_k is not None else ranked


def _group_by_semantics(messages: list[RagMessage]) -> dict[str, list[RagMessage]]:
    grouped: dict[str, list[RagMessage]] = defaultdict(list)
    for msg in messages:
        key = msg.rag_semantic_key or build_semantic_key(
            msg.message_text,
            msg.rag_category or "общее",
        )
        grouped[key].append(msg)
    return grouped


async def systematize_rag(session: AsyncSession, chat_id: int) -> int:
    """Пересобирает классификацию и сводки по всем RAG-записям чата."""
    messages = await get_all_rag_messages(session, chat_id)
    if not messages:
        return 0

    changed = 0
    for msg in messages:
        normalized = _normalize_text(msg.message_text)
        category = classify_rag_message(normalized)
        semantic_key = build_semantic_key(normalized, category)
        if (
            msg.rag_category != category
            or msg.rag_semantic_key != semantic_key
            or msg.rag_canonical_text is None
        ):
            changed += 1
        msg.message_text = normalized
        msg.rag_category = category
        msg.rag_semantic_key = semantic_key
        # Проставляем TTL для старых записей без expires_at
        if msg.expires_at is None and msg.created_at:
            msg.expires_at = msg.created_at + timedelta(days=_DEFAULT_RAG_TTL_DAYS)

    grouped = _group_by_semantics(messages)
    for group_messages in grouped.values():
        canonical = build_canonical_text([msg.message_text for msg in group_messages])
        for msg in group_messages:
            if msg.rag_canonical_text != canonical:
                msg.rag_canonical_text = canonical
                changed += 1

    await session.flush()
    return changed


def format_rag_context(messages: list[RagMessage]) -> str:
    """Форматирует RAG-контекст: сначала компактные группы, затем источники."""
    if not messages:
        return ""
    unique_by_key: dict[str, RagMessage] = {}
    for msg in messages:
        key = msg.rag_semantic_key or str(msg.id)
        unique_by_key.setdefault(key, msg)

    parts: list[str] = []
    for idx, msg in enumerate(unique_by_key.values(), 1):
        category = msg.rag_category or "общее"
        knowledge = msg.rag_canonical_text or msg.message_text
        parts.append(f"[{idx}] ({category}) {knowledge}")
    return "\n".join(parts)


async def build_rag_context(
    session: AsyncSession,
    *,
    chat_id: int,
    query: str,
    top_k: int = 8,
) -> str:
    """Подготавливает компактный релевантный контекст для ответа ассистента."""
    messages = await get_all_rag_messages(session, chat_id)
    ranked = rank_rag_messages(messages, query=query)
    return format_rag_context(ranked[:top_k])


async def search_rag(
    session: AsyncSession,
    chat_id: int,
    query: str,
    top_k: int = 5,
) -> list[RagMessage]:
    """Ищет наиболее релевантные сообщения из RAG-базы."""
    if top_k <= 0:
        return []
    messages = await get_all_rag_messages(session, chat_id)
    return rank_rag_messages(messages, query=query, top_k=top_k)


async def get_rag_count(session: AsyncSession, chat_id: int) -> int:
    """Возвращает количество записей в RAG-базе."""
    from sqlalchemy import func

    result = await session.scalar(
        select(func.count()).select_from(RagMessage).where(RagMessage.chat_id == chat_id)
    )
    return int(result or 0)


async def cleanup_expired_rag(session: AsyncSession) -> int:
    """Удаляет истёкшие RAG-записи."""
    from sqlalchemy import delete

    now = datetime.utcnow()
    result = await session.execute(
        delete(RagMessage).where(
            and_(
                RagMessage.expires_at.isnot(None),
                RagMessage.expires_at < now,
            )
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def extend_rag_ttl(
    session: AsyncSession,
    message_id: int,
    extra_days: int = 90,
) -> bool:
    """Продлевает TTL конкретной RAG-записи."""
    msg = await session.get(RagMessage, message_id)
    if msg is None:
        return False
    msg.expires_at = datetime.utcnow() + timedelta(days=extra_days)
    await session.flush()
    return True
