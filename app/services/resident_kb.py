"""Почему: каноническая база знаний ЖК даёт стабильные и точные ответы без выдумок."""

from __future__ import annotations

import hashlib
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
    # Нормальные формы, всплывающие после лемматизации («нашем» -> «наш»)
    "наш", "ваш", "мой", "свой", "весь", "этот", "тот", "такой",
    "быть", "мочь", "хотеть", "делать", "сказать", "подсказать",
    # Короткие предлоги/союзы (длина 2 проходит фильтр _tokenize)
    "на", "не", "но", "от", "до", "из", "за", "под", "над", "при",
    "про", "же", "бы", "во", "со", "об", "он", "мы", "вы", "их", "им",
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
    # Паспорт достоверности: когда факт проверялся в последний раз (ISO-дата)
    verified_at: str | None = None


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
    # Лемматизация: запрос «про пропуска» находит запись «пропуск» (падежи/число).
    # Стоп-слова фильтруем и до, и после — «нашем» проходит первый фильтр,
    # но его лемма «наш» должна отсеяться.
    from app.utils.morphology import lemmatize
    lemmas = {lemmatize(t) for t in _tokenize(text) if t not in _STOP_WORDS}
    return {l for l in lemmas if l not in _STOP_WORDS}


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


def _entry_head_tokens(entry: ResidentKbEntry) -> set[str]:
    """Токены «шапки» записи — то, о чём запись (формулировки, теги, категория)."""
    chunks = [
        " ".join(entry.question_patterns),
        " ".join(entry.search_tags),
        " ".join(entry.aliases),
        entry.category,
    ]
    return _content_tokens(" ".join(chunks))


# Совпадение только в тексте ответа весит вдвое меньше совпадения в шапке:
# «УК» упоминается в десятках ответов («обратитесь в УК»), и раньше вопрос
# про часы работы УК тянул в контекст лифт, шлагбаум и аварийку.
_ANSWER_ONLY_WEIGHT = 0.5

# Кэш предрассчитанных токенов записей KB — заполняется при первом обращении
_ENTRY_TOKEN_CACHE: dict[str, set[str]] = {}
_ENTRY_HEAD_TOKEN_CACHE: dict[str, set[str]] = {}


def _get_cached_entry_tokens(entry: ResidentKbEntry) -> set[str]:
    """Возвращает токены записи KB из кэша или вычисляет и кэширует."""
    if entry.id not in _ENTRY_TOKEN_CACHE:
        _ENTRY_TOKEN_CACHE[entry.id] = _entry_tokens(entry)
    return _ENTRY_TOKEN_CACHE[entry.id]


def _get_cached_head_tokens(entry: ResidentKbEntry) -> set[str]:
    if entry.id not in _ENTRY_HEAD_TOKEN_CACHE:
        _ENTRY_HEAD_TOKEN_CACHE[entry.id] = _entry_head_tokens(entry)
    return _ENTRY_HEAD_TOKEN_CACHE[entry.id]


def _score_entry(query_tokens: set[str], entry: ResidentKbEntry) -> float:
    if not query_tokens:
        return 0.0

    entry_tokens = _get_cached_entry_tokens(entry)
    overlap = query_tokens & entry_tokens
    overlap_count = len(overlap)
    if overlap_count == 0:
        return 0.0

    # Общие слова, которые не должны считаться значимым совпадением
    # После лемматизации токены запроса приходят в нормальной форме,
    # поэтому и этот список — в леммах.
    _GENERIC_TOKENS = {
        "какой", "есть", "рядом", "жк", "дом", "квартира", "район",
        "сколько", "почему", "зачем", "откуда", "ответ", "вопрос",
        "наш", "ваш", "хороший", "новый",
    }
    meaningful_overlap = overlap - _GENERIC_TOKENS
    meaningful_count = len(meaningful_overlap)

    # Если совпадают только общие слова — не считаем это релевантным ответом
    if meaningful_count == 0 and overlap_count <= 2:
        return 0.0

    head_overlap = overlap & _get_cached_head_tokens(entry)
    weighted_overlap = len(head_overlap) + _ANSWER_ONLY_WEIGHT * (overlap_count - len(head_overlap))
    overlap_ratio = weighted_overlap / max(len(query_tokens), 1)

    # Двухуровневые keyword бонусы — только за ОБЩЕЕ ключевое слово. Раньше
    # хватало любого сильного слова в вопросе и любого в записи («ук» в вопросе
    # + «лифт» в записи = +0.15), и почти каждая запись проходила порог.
    keyword_bonus = 0.0
    if overlap & _CRITICAL_KEYWORDS:
        keyword_bonus = 0.3
    elif overlap & _STRONG_KEYWORDS:
        keyword_bonus = 0.15

    category_bonus = (
        0.08 if head_overlap and entry.category in {"шлагбаум", "ук", "аварийка"} else 0.0
    )
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


def _newest_date(path: Path) -> str:
    """Самая свежая дата правки/проверки в файле базы («» — файл битый)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return max(
            (str(item.get(key) or "")[:10] for item in raw for key in ("updated_at", "verified_at")),
            default="",
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def resolve_kb_path(root: Path | None = None) -> Path | None:
    """Какой файл базы читать: копию в data/ или файл из образа (kb/).

    Почему не «data/ всегда»: на сервере data/ — bind mount, и копия базы,
    однажды положенная туда руками, навсегда перекрыла бы файл из образа —
    ни одна правка из репозитория не доехала бы до бота. Поэтому берём
    файл с более свежими датами; при равенстве — data/ (ручная правка на
    сервере поверх той же версии).
    """
    project_root = root or Path(__file__).resolve().parents[2]
    server_copy = project_root / "data" / "resident_kb.json"
    image_copy = project_root / "kb" / "resident_kb.json"
    if not server_copy.exists():
        return image_copy if image_copy.exists() else None
    if not image_copy.exists():
        return server_copy
    if _newest_date(image_copy) > _newest_date(server_copy):
        logger.warning(
            "data/resident_kb.json старше файла из образа — читаем %s. "
            "Удалите устаревшую копию с сервера.", image_copy,
        )
        return image_copy
    return server_copy


def kb_fingerprint() -> str:
    """Отпечаток содержимого базы: меняется при любой правке файла."""
    path = resolve_kb_path()
    if path is None:
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@lru_cache(maxsize=1)
def load_resident_kb() -> tuple[ResidentKbEntry, ...]:
    _ENTRY_TOKEN_CACHE.clear()
    _ENTRY_HEAD_TOKEN_CACHE.clear()
    kb_path = resolve_kb_path()
    if kb_path is None:
        logger.warning("Файл базы знаний не найден ни в data/, ни в kb/.")
        return ()
    raw = json.loads(kb_path.read_text(encoding="utf-8"))
    entries: list[ResidentKbEntry] = []
    for item in raw:
        entries.append(ResidentKbEntry(**item))
    logger.info(
        "Resident KB loaded: %s entries from %s, loaded_at=%s",
        len(entries), kb_path, datetime.now(timezone.utc).isoformat(),
    )
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


def get_entries_by_category(category: str, *, limit: int = 5) -> list[ResidentKbEntry]:
    """Возвращает записи KB одной категории, отсортированные по приоритету."""

    if not category:
        return []
    normalized = category.lower().strip()
    matched = [e for e in load_resident_kb() if e.category.lower().strip() == normalized]
    matched.sort(key=lambda e: (-e.priority, e.id))
    return matched[:limit]


def build_resident_context(query: str, *, context: list[str] | None = None, top_k: int = 6) -> str:
    result = search_resident_kb(query, context=context, top_k=top_k)
    if not result.matches:
        return ""
    # Отсекаем записи с низкой релевантностью, чтобы не загрязнять контекст ИИ
    _MIN_CONTEXT_SCORE = 0.45
    # Записи вдвое слабее лучшей — шум: забирают бюджет контекста (4000
    # символов на все источники) у RAG/FAQ/карточек мест и сбивают модель.
    # На офлайн-наборе 65 вопросов это 2.4 записи в контексте вместо 4.5
    # без потери нужной записи.
    _RELATIVE_CUTOFF = 0.5
    best = result.matches[0].score
    relevant = [
        m for m in result.matches
        if m.score >= _MIN_CONTEXT_SCORE and m.score >= best * _RELATIVE_CUTOFF
    ]
    if not relevant:
        return ""
    parts: list[str] = []
    seen_ids: set[str] = set()
    for idx, match in enumerate(relevant, start=1):
        # Не дублируем записи
        if match.entry.id in seen_ids:
            continue
        seen_ids.add(match.entry.id)
        if match.score >= 0.8:
            relevance = "высокая"
        elif match.score >= 0.6:
            relevance = "средняя"
        else:
            relevance = "низкая"
        parts.append(
            f"[{idx}] Категория: {match.entry.category} | Релевантность: {relevance}\n"
            f"{match.entry.answer}"
        )
    return "\n\n".join(parts)
