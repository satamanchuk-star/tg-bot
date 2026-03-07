"""Почему: каноническая база знаний ЖК даёт стабильные и точные ответы без выдумок."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_STOP_WORDS = {
    "как", "что", "где", "когда", "если", "или", "для", "это", "через",
    "нужно", "можно", "чтобы", "только", "кто", "куда", "по", "ли", "а",
}

_STRONG_KEYWORDS = {
    "шлагбаум", "дворецкий", "пропуск", "гостевой", "лифт", "лифте", "лифту",
    "аварийка", "аварийную", "ук", "век", "видеонаблюдение", "камер", "гранлайн",
    "мособлеирц", "показания", "перерасчет", "перерасчёт", "лифтек",
}


@dataclass(slots=True)
class ResidentKbEntry:
    id: str
    category: str
    question_patterns: list[str]
    answer: str
    search_tags: list[str]
    priority: int
    aliases: list[str]
    source: str
    updated_at: str


@dataclass(slots=True)
class ResidentKbMatch:
    entry: ResidentKbEntry
    score: float


@dataclass(slots=True)
class ResidentKbSearchResult:
    matches: list[ResidentKbMatch]
    exact: bool


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")) if len(t) >= 2]


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokenize(text) if t not in _STOP_WORDS}


def _normalize_query(text: str) -> str:
    compact = " ".join(text.split())
    compact = re.sub(r"^/ai(?:@\w+)?\s*", "", compact, flags=re.IGNORECASE)
    compact = re.sub(r"@\w+", "", compact)
    return " ".join(compact.split())[:1000]


def _is_short_followup(query: str) -> bool:
    tokens = _tokenize(query)
    if len(tokens) > 6:
        return False
    lowered = query.lower().strip()
    return lowered.startswith(("а ", "а по", "и ", "а если", "по воде", "по свету", "когда"))


def enrich_query_with_context(query: str, context: list[str]) -> str:
    normalized = _normalize_query(query)
    if not normalized or not context:
        return normalized
    if not _is_short_followup(normalized):
        return normalized

    previous_user = ""
    for row in reversed(context):
        if row.startswith("user:"):
            previous_user = row.split(":", 1)[1].strip()
            if previous_user:
                break
    if not previous_user:
        return normalized
    return f"{previous_user}. {normalized}"[:1000]


def _entry_tokens(entry: ResidentKbEntry) -> set[str]:
    chunks = [
        entry.answer,
        " ".join(entry.question_patterns),
        " ".join(entry.search_tags),
        " ".join(entry.aliases),
        entry.category,
    ]
    return _content_tokens(" ".join(chunks))


def _score_entry(query_tokens: set[str], entry: ResidentKbEntry) -> float:
    if not query_tokens:
        return 0.0

    entry_tokens = _entry_tokens(entry)
    overlap = len(query_tokens & entry_tokens)
    if overlap == 0:
        return 0.0

    overlap_ratio = overlap / max(len(query_tokens), 1)
    keyword_bonus = 0.0
    if any(keyword in query_tokens for keyword in _STRONG_KEYWORDS) and any(
        keyword in entry_tokens for keyword in _STRONG_KEYWORDS
    ):
        keyword_bonus = 0.2

    category_bonus = 0.08 if entry.category in {"шлагбаум", "ук", "аварийка"} else 0.0
    priority_bonus = min(entry.priority, 100) / 250
    return overlap_ratio + keyword_bonus + category_bonus + priority_bonus


def _is_exact_match(normalized_query: str, entry: ResidentKbEntry) -> bool:
    lowered = normalized_query.lower()
    patterns = [*entry.question_patterns, *entry.aliases, *entry.search_tags]
    return any(pattern.lower() in lowered for pattern in patterns)


@lru_cache(maxsize=1)
def load_resident_kb() -> tuple[ResidentKbEntry, ...]:
    kb_path = Path(__file__).resolve().parents[2] / "data" / "resident_kb.json"
    raw = json.loads(kb_path.read_text(encoding="utf-8"))
    entries: list[ResidentKbEntry] = []
    for item in raw:
        entries.append(ResidentKbEntry(**item))
    logger.info("Resident KB loaded: %s entries, updated_at=%s", len(entries), datetime.utcnow().isoformat())
    return tuple(entries)


def search_resident_kb(query: str, *, context: list[str] | None = None, top_k: int = 4) -> ResidentKbSearchResult:
    context_rows = context or []
    normalized_query = enrich_query_with_context(query, context_rows)
    query_tokens = _content_tokens(normalized_query)
    if not query_tokens:
        return ResidentKbSearchResult(matches=[], exact=False)

    scored: list[ResidentKbMatch] = []
    exact = False
    for entry in load_resident_kb():
        score = _score_entry(query_tokens, entry)
        if score <= 0:
            continue
        if _is_exact_match(normalized_query, entry):
            score += 0.45
            exact = True
        scored.append(ResidentKbMatch(entry=entry, score=score))

    scored.sort(key=lambda item: (-item.score, -item.entry.priority, item.entry.id))
    return ResidentKbSearchResult(matches=scored[:top_k], exact=exact)


def _style_answer(base: str, *, category: str) -> str:
    if category == "лифт":
        return f"По лифту лучше сразу так: {base}"
    if category == "аварийка":
        return f"Если ситуация срочная, лучше не ждать: {base}"
    return base


def build_resident_answer(query: str, *, context: list[str] | None = None) -> str | None:
    result = search_resident_kb(query, context=context, top_k=3)
    if not result.matches:
        return None

    best = result.matches[0]
    if best.score < 0.6:
        return None

    # Если есть несколько близких ответов, объединяем короткой выжимкой.
    close_matches = [m for m in result.matches if m.score >= best.score - 0.15]
    if len(close_matches) == 1:
        return _style_answer(close_matches[0].entry.answer, category=close_matches[0].entry.category)

    unique_answers: list[str] = []
    for item in close_matches:
        if item.entry.answer not in unique_answers:
            unique_answers.append(item.entry.answer)
    return " ".join(unique_answers[:2])[:800]


def build_resident_context(query: str, *, context: list[str] | None = None, top_k: int = 6) -> str:
    result = search_resident_kb(query, context=context, top_k=top_k)
    if not result.matches:
        return ""
    parts: list[str] = []
    for idx, match in enumerate(result.matches, start=1):
        parts.append(f"[{idx}] ({match.entry.category}) {match.entry.answer}")
    return "\n".join(parts)
