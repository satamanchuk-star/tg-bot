"""Почему: каноническая база знаний ЖК даёт стабильные и точные ответы без выдумок."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_STOP_WORDS = {
    "как", "что", "где", "когда", "если", "или", "для", "это", "через",
    "нужно", "можно", "чтобы", "только", "кто", "куда", "по", "ли", "а",
}

_CRITICAL_KEYWORDS = {
    "шлагбаум", "дворецкий", "пропуск", "гостевой",
    "аварийка", "аварийную", "затопило", "протечка",
    "лифт", "лифте", "застрял",
    "едс", "112",
}  # +0.3

_STRONG_KEYWORDS = {
    "ук", "век", "управляющая",
    "видеонаблюдение", "камер", "крепость24",
    "гранлайн", "интернет",
    "мособлеирц", "показания", "счётчик",
    "участковый", "полиция",
    "правила", "поликлиника",
    "школа", "садик", "метро",
    "парковка", "мусор", "домофон",
    "отопление", "вода", "батарея",
    "лифтек", "мосэнергосбыт", "электричество",
    "перерасчет", "перерасчёт", "счетчик",
    "провайдер", "авария",
    "травмпункт", "врач", "скорая", "стоматология",
    "садик", "детский",
    "автобус", "электричка", "мцд", "транспорт",
    "тишина", "шум", "ремонт",
    "мфц", "администрация",
    "аптека", "почта",
    "газон", "тко",
    "добродел", "жалоба", "гжи", "экстренные",
}  # +0.15


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


# Кэш предрассчитанных токенов записей KB — заполняется при первом обращении
_ENTRY_TOKEN_CACHE: dict[str, set[str]] = {}


def _get_cached_entry_tokens(entry: ResidentKbEntry) -> set[str]:
    """Возвращает токены записи KB из кэша или вычисляет и кэширует."""
    if entry.id not in _ENTRY_TOKEN_CACHE:
        _ENTRY_TOKEN_CACHE[entry.id] = _entry_tokens(entry)
    return _ENTRY_TOKEN_CACHE[entry.id]


def _score_entry(query_tokens: set[str], entry: ResidentKbEntry) -> float:
    if not query_tokens:
        return 0.0

    entry_tokens = _get_cached_entry_tokens(entry)
    overlap = query_tokens & entry_tokens
    overlap_count = len(overlap)
    if overlap_count == 0:
        return 0.0

    # Общие слова, которые не должны считаться значимым совпадением
    _GENERIC_TOKENS = {
        "какие", "какой", "какая", "какое", "есть", "рядом", "жк",
        "дом", "доме", "квартира", "квартиры", "район", "районе",
        "сколько", "почему", "зачем", "откуда", "ответ", "вопрос",
    }
    meaningful_overlap = overlap - _GENERIC_TOKENS
    meaningful_count = len(meaningful_overlap)

    # Если совпадают только общие слова — не считаем это релевантным ответом
    if meaningful_count == 0 and overlap_count <= 2:
        return 0.0

    overlap_ratio = overlap_count / max(len(query_tokens), 1)

    # Двухуровневые keyword бонусы
    keyword_bonus = 0.0
    if any(kw in query_tokens for kw in _CRITICAL_KEYWORDS) and any(
        kw in entry_tokens for kw in _CRITICAL_KEYWORDS
    ):
        keyword_bonus = 0.3
    elif any(kw in query_tokens for kw in _STRONG_KEYWORDS) and any(
        kw in entry_tokens for kw in _STRONG_KEYWORDS
    ):
        keyword_bonus = 0.15

    category_bonus = 0.08 if entry.category in {"шлагбаум", "ук", "аварийка"} else 0.0
    # Приоритет влияет меньше, чтобы не вытягивать нерелевантные записи
    priority_bonus = min(entry.priority, 100) / 500
    return overlap_ratio + keyword_bonus + category_bonus + priority_bonus


def _bounded_levenshtein(a: str, b: str, max_dist: int = 2) -> int:
    """Ограниченное расстояние Левенштейна: если dist > max_dist — возвращает max_dist+1."""
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        new_dp = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            new_dp[j] = min(dp[j] + 1, new_dp[j - 1] + 1, dp[j - 1] + (0 if ca == cb else 1))
        dp = new_dp
        if min(dp) > max_dist:
            return max_dist + 1
    return dp[len(b)]


def _is_exact_match(normalized_query: str, entry: ResidentKbEntry) -> bool:
    lowered = normalized_query.lower()
    patterns = [*entry.question_patterns, *entry.aliases, *entry.search_tags]
    for pattern in patterns:
        pattern_lower = pattern.lower()
        # Точное вхождение
        if pattern_lower in lowered:
            return True
        # Fuzzy: Левенштейн ≤ 2 для длинных слов (≥ 6 символов)
        if len(pattern_lower) >= 6:
            for word in _tokenize(normalized_query):
                if len(word) >= 5 and _bounded_levenshtein(word, pattern_lower) <= 2:
                    return True
    return False


@lru_cache(maxsize=1)
def load_resident_kb() -> tuple[ResidentKbEntry, ...]:
    _ENTRY_TOKEN_CACHE.clear()
    project_root = Path(__file__).resolve().parents[2]
    kb_path = project_root / "data" / "resident_kb.json"
    if not kb_path.exists():
        # Fallback: файл может лежать в неперекрытом каталоге kb/ внутри образа
        kb_path = project_root / "kb" / "resident_kb.json"
    if not kb_path.exists():
        logger.warning("Файл базы знаний не найден: %s", kb_path)
        return ()
    raw = json.loads(kb_path.read_text(encoding="utf-8"))
    entries: list[ResidentKbEntry] = []
    for item in raw:
        entries.append(ResidentKbEntry(**item))
    logger.info("Resident KB loaded: %s entries, updated_at=%s", len(entries), datetime.now(timezone.utc).isoformat())
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
    # Ответы из KB уже содержат структуру и эмодзи, не добавляем лишнее
    return base


def build_resident_answer(query: str, *, context: list[str] | None = None) -> str | None:
    result = search_resident_kb(query, context=context, top_k=4)
    if not result.matches:
        return None

    best = result.matches[0]
    if best.score < 0.6:
        return None

    # Если есть несколько близких ответов из разных категорий — объединяем.
    close_matches = [m for m in result.matches if m.score >= best.score - 0.15]
    if len(close_matches) == 1:
        return _style_answer(close_matches[0].entry.answer, category=close_matches[0].entry.category)

    # Объединяем ответы из разных категорий, убирая дубли
    seen_categories: set[str] = set()
    unique_answers: list[str] = []
    for item in close_matches:
        # Не дублируем ответы из одной категории
        if item.entry.category in seen_categories:
            continue
        seen_categories.add(item.entry.category)
        unique_answers.append(item.entry.answer)

    if len(unique_answers) == 1:
        return unique_answers[0]

    # Разделяем ответы визуально для читаемости
    return "\n\n".join(unique_answers[:2])[:1200]


def build_resident_context(query: str, *, context: list[str] | None = None, top_k: int = 6) -> str:
    result = search_resident_kb(query, context=context, top_k=top_k)
    if not result.matches:
        return ""
    # Отсекаем записи с низкой релевантностью, чтобы не загрязнять контекст ИИ
    _MIN_CONTEXT_SCORE = 0.35
    relevant = [m for m in result.matches if m.score >= _MIN_CONTEXT_SCORE]
    if not relevant:
        return ""
    parts: list[str] = []
    seen_ids: set[str] = set()
    for idx, match in enumerate(relevant, start=1):
        # Не дублируем записи
        if match.entry.id in seen_ids:
            continue
        seen_ids.add(match.entry.id)
        relevance = "высокая" if match.score >= 0.8 else "средняя"
        parts.append(
            f"[{idx}] Категория: {match.entry.category} | Релевантность: {relevance}\n"
            f"{match.entry.answer}"
        )
    return "\n\n".join(parts)
