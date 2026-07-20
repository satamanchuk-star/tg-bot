"""Почему: страницы квиз-сайтов (viktorinavopros.ru, raznoeinteresnoe.ru,
quizvopros.ru) имеют единый формат: нумерованный список ВОПРОСОВ, затем ссылка
«Ответы»/«Скрыть ответ», затем нумерованный список ОТВЕТОВ. Парсер сопоставляет
пары по номерам детерминированно — без ИИ, воспроизводимо и бесплатно.

Использование (там, где есть доступ к сайтам — например на сервере или локально):
    python -m scripts.parse_quiz_page page1.html page2.html   # сохранённые страницы
    python -m scripts.parse_quiz_page https://…/вопросы/      # или URL
Печатает JSON-массив валидных пар (кривые ответы отсеивает валидатор) —
дальше их можно влить в data/quiz_questions.json и прогнать тесты.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Маркеры границы «вопросы → ответы» (по вёрстке сайтов со скриншотов).
_ANSWER_MARKERS = re.compile(
    r"^\s*(ответы|скрыть ответ|показать ответ|показать ответы)\s*[:.]?\s*$",
    re.IGNORECASE,
)
# Нумерованный пункт: «1. Текст…» / «1) Текст…»
_ITEM_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(.+)$")
# Заголовок части («Часть 100») — граница блока: не приклеивать к пункту.
_PART_RE = re.compile(r"^\s*часть\s+\d+\s*$", re.IGNORECASE)


def _html_to_lines(html: str) -> list[str]:
    """HTML → читаемые строки (теги/скрипты выкинуты)."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:
        # Без bs4 (или это уже голый текст) — грубая зачистка тегов.
        text = re.sub(r"<[^>]+>", "\n", html)
    return [ln.strip() for ln in text.splitlines()]


def _collect_numbered(lines: list[str], start: int, stop_at_marker: bool) -> tuple[dict[int, str], int]:
    """Собирает нумерованные пункты с позиции start.

    Многострочные пункты склеиваются (продолжение — строки без номера).
    Возвращает (номер→текст, индекс строки, где остановились).
    """
    items: dict[int, str] = {}
    current_num: int | None = None
    i = start
    while i < len(lines):
        line = lines[i]
        if stop_at_marker and _ANSWER_MARKERS.match(line):
            break
        if _PART_RE.match(line):
            break  # началась новая «Часть» — блок закончился
        m = _ITEM_RE.match(line)
        if m:
            num = int(m.group(1))
            # Номер «1» после уже собранных пунктов = начался новый блок.
            if num == 1 and items and current_num is not None and current_num >= 2:
                break
            current_num = num
            items[num] = m.group(2).strip()
        elif current_num is not None and line:
            # Продолжение многострочного пункта.
            items[current_num] += " " + line
        elif current_num is not None and not line:
            pass  # пустая строка внутри блока — не завершает пункт
        i += 1
    return items, i


def parse_page(html: str, category: str = "квиз") -> list[dict]:
    """Извлекает пары «вопрос-ответ» из страницы формата «вопросы → Ответы → ответы».

    На странице может быть несколько частей (Часть 99, Часть 100…) — каждая со
    своим блоком вопросов и ответов; парсер идёт по всем.
    """
    lines = _html_to_lines(html)
    pairs: list[dict] = []
    i = 0
    while i < len(lines):
        # Ищем начало блока вопросов: строку-пункт «1. …»
        m = _ITEM_RE.match(lines[i])
        if not (m and int(m.group(1)) == 1):
            i += 1
            continue
        questions, i = _collect_numbered(lines, i, stop_at_marker=True)
        # Пропускаем строки до маркера ответов (если он есть рядом).
        marker_found = False
        lookahead = i
        while lookahead < len(lines) and lookahead - i < 10:
            if _ANSWER_MARKERS.match(lines[lookahead]):
                marker_found = True
                lookahead += 1
                break
            if _ITEM_RE.match(lines[lookahead]):
                break  # сразу начался нумерованный блок — считаем его ответами
            lookahead += 1
        i = lookahead
        answers, i = _collect_numbered(lines, i, stop_at_marker=False)

        if not marker_found and not answers:
            continue  # это был не вопросный блок
        for num, question in sorted(questions.items()):
            answer = answers.get(num)
            if not answer:
                continue
            pairs.append({
                "question": question.strip(),
                "answer": answer.strip().rstrip("."),
                "category": category,
            })
    return pairs


def filter_valid(pairs: list[dict]) -> tuple[list[dict], list[str]]:
    """Пропускает пары через игровой валидатор: в банк идут только те, чей
    ответ короткий и засчитывается матчем сам себе."""
    from scripts.validate_quiz import validate_one

    valid: list[dict] = []
    rejected: list[str] = []
    for p in pairs:
        issues = validate_one(p)
        if issues:
            rejected.append(f"«{p['question'][:60]}» → «{p['answer'][:40]}»: {issues[0]}")
        else:
            valid.append(p)
    return valid, rejected


def extract_post_links(html: str) -> list[str]:
    """Ссылки на посты со страницы-каталога (WordPress: заголовки статей) +
    пагинация «Older Posts». Каталог сам пар не содержит — только анонсы."""
    links: list[str] = []
    seen: set[str] = set()
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # Заголовки статей и кнопки «Read Post».
        for sel in ("h2.entry-title a", "a.continue-reading", "h1.entry-title a"):
            for a in soup.select(sel):
                href = a.get("href")
                if href and href.startswith("http") and href not in seen:
                    seen.add(href)
                    links.append(href)
        # Пагинация «Older Posts» — чтобы обойти весь раздел.
        for a in soup.select("div.link-prev a, a.next, .blog-nav a"):
            href = a.get("href")
            if href and href.startswith("http") and href not in seen:
                seen.add(href)
                links.append(href)
    except Exception:
        pass
    return links


def _read_source(src: str) -> str:
    if src.startswith("http"):
        import httpx
        resp = httpx.get(src, timeout=20, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        resp.raise_for_status()
        return resp.text
    return Path(src).read_text(encoding="utf-8")


def _is_pagination(url: str) -> bool:
    return "/page/" in url


def crawl(sources: list[str], max_pages: int = 60) -> list[dict]:
    """Обходит источники: файл/URL с парами берётся как есть; страница-каталог
    раскрывается в посты (и пагинацию) с вежливой паузой между запросами."""
    import time

    all_pairs: list[dict] = []
    queue = list(sources)
    visited: set[str] = set()
    fetched = 0
    while queue and fetched < max_pages:
        src = queue.pop(0)
        if src in visited:
            continue
        visited.add(src)
        try:
            html = _read_source(src)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ {src}: {exc}", file=sys.stderr)
            continue
        fetched += 1
        found = parse_page(html)
        if found:
            print(f"{src}: пар {len(found)}", file=sys.stderr)
            all_pairs.extend(found)
        else:
            # Пар нет — возможно, это каталог: раскрываем посты и пагинацию.
            links = extract_post_links(html)
            posts = [u for u in links if not _is_pagination(u)]
            pages = [u for u in links if _is_pagination(u)]
            if links:
                print(f"{src}: каталог — постов {len(posts)}, пагинация {len(pages)}",
                      file=sys.stderr)
            queue.extend(posts + pages)
        if src.startswith("http"):
            time.sleep(1.0)  # вежливая пауза
    return all_pairs


if __name__ == "__main__":
    all_pairs = crawl(sys.argv[1:])
    valid, rejected = filter_valid(all_pairs)
    for line in rejected:
        print("отсеяно:", line, file=sys.stderr)
    print(f"валидных: {len(valid)}, отсеяно: {len(rejected)}", file=sys.stderr)
    print(json.dumps(valid, ensure_ascii=False, indent=2))
