"""Почему: лемматизация выравнивает падежи в поиске — «про шлагбаума» находит
запись «шлагбаум». pymorphy3 опционален: без него поиск работает как раньше.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_MORPH = None
_MORPH_FAILED = False


def _get_morph():
    """Ленивый singleton MorphAnalyzer (инициализация ~1 сек, только раз)."""
    global _MORPH, _MORPH_FAILED
    if _MORPH is None and not _MORPH_FAILED:
        try:
            import pymorphy3
            _MORPH = pymorphy3.MorphAnalyzer()
            logger.info("Морфология: pymorphy3 инициализирован.")
        except Exception:
            _MORPH_FAILED = True
            logger.warning("pymorphy3 недоступен — поиск работает без лемматизации.")
    return _MORPH


@lru_cache(maxsize=50_000)
def lemmatize(word: str) -> str:
    """Возвращает нормальную форму слова («шлагбаума» → «шлагбаум»).

    Числа, латиница и всё, что анализатор не знает, возвращаются как есть.
    Результат приводится к «е» вместо «ё» — как остальная нормализация поиска.
    """
    if not word or word.isdigit():
        return word
    morph = _get_morph()
    if morph is None:
        return word
    try:
        parsed = morph.parse(word)
        if not parsed:
            return word
        return parsed[0].normal_form.replace("ё", "е")
    except Exception:
        return word
