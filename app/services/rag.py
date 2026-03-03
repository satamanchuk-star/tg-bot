"""RAG-сервис: накопление, систематизация и поиск знаний чата ЖК.

Почему: админы добавляют сырые сообщения, а бот хранит и объединяет их
в компактные смысловые блоки для более точных ответов.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
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
}

_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("парковка", ("парков", "шлагбаум", "машин", "мест")),
    ("лифт", ("лифт", "подъезд", "этаж")),
    ("ук", ("ук", "управля", "диспетчер", "заявк")),
    ("коммуналка", ("вода", "свет", "отоплен", "счетчик")),
    ("безопасность", ("охран", "камера", "пропуск", "консьерж")),
]


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[а-яёa-z0-9]+", text.lower()) if len(w) >= 3]


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


async def add_rag_message(
    session: AsyncSession,
    *,
    chat_id: int,
    message_text: str,
    added_by_user_id: int,
    source_user_id: int | None = None,
    source_message_id: int | None = None,
) -> RagMessage:
    """Добавляет сообщение в RAG-базу и сразу классифицирует его."""
    cleaned = _normalize_text(message_text)
    category = classify_rag_message(cleaned)
    semantic_key = build_semantic_key(cleaned, category)
    record = RagMessage(
        chat_id=chat_id,
        message_text=cleaned,
        added_by_user_id=added_by_user_id,
        source_user_id=source_user_id,
        source_message_id=source_message_id,
        rag_category=category,
        rag_semantic_key=semantic_key,
        rag_canonical_text=cleaned,
        created_at=datetime.utcnow(),
    )
    session.add(record)
    await session.flush()
    return record


async def get_all_rag_messages(session: AsyncSession, chat_id: int) -> list[RagMessage]:
    """Возвращает все сообщения RAG для чата в хронологическом порядке."""
    result = await session.execute(
        select(RagMessage)
        .where(RagMessage.chat_id == chat_id)
        .order_by(RagMessage.created_at.asc())
    )
    return list(result.scalars().all())


def rank_rag_messages(
    messages: list[RagMessage],
    *,
    query: str,
    top_k: int | None = None,
) -> list[RagMessage]:
    """Сортирует RAG-сообщения: сначала релевантные запросу, затем остальные."""
    query_tokens = _token_set(query)
    if not messages:
        return []
    if not query_tokens:
        return messages[:top_k] if top_k is not None else messages

    scored: list[tuple[RagMessage, float]] = []
    untouched: list[RagMessage] = []
    for msg in messages:
        msg_tokens = _token_set(msg.rag_canonical_text or msg.message_text)
        if not msg_tokens:
            untouched.append(msg)
            continue
        overlap = len(query_tokens & msg_tokens)
        if overlap > 0:
            score = overlap / len(query_tokens)
            scored.append((msg, score))
            continue
        untouched.append(msg)

    scored.sort(key=lambda item: item[1], reverse=True)
    ranked = [msg for msg, _ in scored] + untouched
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
