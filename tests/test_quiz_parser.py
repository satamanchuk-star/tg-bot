"""Тесты парсера квиз-страниц: структура «вопросы → Ответы → ответы» со
скриншотов сайтов (viktorinavopros/raznoeinteresnoe/quizvopros)."""

from __future__ import annotations

from scripts.parse_quiz_page import filter_valid, parse_page

# Синтетическая страница, повторяющая вёрстку со скриншотов владельца.
_PAGE = """
<html><body>
<h1>Квиз вопросы</h1>
<p><em>Часть 100</em></p>
<p>1. Этот боксер выбросил в реку свою олимпийскую медаль после того, как его
отказались обслужить в ресторане только для белых. Назовите спортсмена.</p>
<p>2. Этот термин стал широко известен во время эпидемии и показывает
уровень насыщения кислородом крови.</p>
<p>3. В 1992 году США хотели провести летние Олимпийские игры, приурочив их
к круглой дате. Назовите город, в котором могла быть проведена Олимпиада.</p>
<a>Скрыть ответ</a>
<p>1. Мухаммед Али.</p>
<p>2. Сатурация.</p>
<p>3. В 1992 году исполнялось 500 лет с открытия Америки Христофором Колумбом,
а наиболее подходящим городом американцы посчитали Коламбус, названный в честь
мореплавателя. Правда, до официальной заявки дело не дошло.</p>
</body></html>
"""


def test_parses_numbered_pairs_by_index() -> None:
    pairs = parse_page(_PAGE)
    by_q = {p["question"][:20]: p["answer"] for p in pairs}
    assert len(pairs) == 3
    assert any(a == "Мухаммед Али" for a in by_q.values())
    assert any(a == "Сатурация" for a in by_q.values())


def test_validator_rejects_long_explanation_answers() -> None:
    """Ответ №3 со скриншота — целый абзац; в банк такой не должен попасть."""
    pairs = parse_page(_PAGE)
    valid, rejected = filter_valid(pairs)
    assert len(valid) == 2  # Али и Сатурация
    assert len(rejected) == 1
    assert "Коламбус" in rejected[0] or "слишком длинный" in rejected[0]


def test_multipart_page() -> None:
    """Несколько «Частей» на одной странице — каждая со своими ответами."""
    page = """
    Часть 99
    1. Столица Франции?
    2. Самая длинная река Африки?
    Ответы
    1. Париж.
    2. Нил.
    Часть 100
    1. Царь зверей?
    Ответы
    1. Лев.
    """
    pairs = parse_page(page)
    answers = {p["answer"] for p in pairs}
    assert answers == {"Париж", "Нил", "Лев"}


def test_multiline_question_glued() -> None:
    page = """
    1. Очень длинный вопрос,
    который продолжается на второй строке. Назовите ответ.
    Ответы
    1. Ответище.
    """
    pairs = parse_page(page)
    assert len(pairs) == 1
    assert "второй строке" in pairs[0]["question"]


def test_unpaired_numbers_skipped() -> None:
    """Вопрос без ответа с тем же номером — пропускается, не падает."""
    page = """
    1. Вопрос с ответом?
    2. Вопрос без ответа?
    Ответы
    1. Есть.
    """
    pairs = parse_page(page)
    assert len(pairs) == 1
    assert pairs[0]["answer"] == "Есть"


def test_plain_text_without_html() -> None:
    """Работает и по голому тексту (скопированному со страницы)."""
    page = "1. Дважды два?\nОтветы\n1. Четыре."
    pairs = parse_page(page)
    assert pairs == [{"question": "Дважды два?", "answer": "Четыре", "category": "квиз"}]


_CATEGORY_HTML = """
<article><h2 class="entry-title"><a href="https://site.ru/post-1/">Пост 1</a></h2>
<a class="continue-reading" href="https://site.ru/post-1/">Read Post</a></article>
<article><h2 class="entry-title"><a href="https://site.ru/post-2/">Пост 2</a></h2></article>
<div class="blog-nav"><div class="link-prev">
<a href="https://site.ru/category/x/page/2/">Older Posts</a></div></div>
"""


def test_extract_post_links_from_category() -> None:
    """Каталог WordPress: ссылки постов из заголовков + пагинация, без дублей."""
    from scripts.parse_quiz_page import extract_post_links

    links = extract_post_links(_CATEGORY_HTML)
    assert links == [
        "https://site.ru/post-1/",
        "https://site.ru/post-2/",
        "https://site.ru/category/x/page/2/",
    ]


def test_crawl_follows_category_to_posts(monkeypatch) -> None:
    """Обход: каталог → посты → пары; пагинация тоже посещается."""
    import scripts.parse_quiz_page as pq

    pages = {
        "https://site.ru/category/x/": _CATEGORY_HTML,
        "https://site.ru/post-1/": "1. Столица Франции?\nОтветы\n1. Париж.",
        "https://site.ru/post-2/": "1. Царь зверей?\nОтветы\n1. Лев.",
        "https://site.ru/category/x/page/2/": "<p>пусто</p>",
    }
    visited = []

    def _fake_read(src):
        visited.append(src)
        return pages[src]

    monkeypatch.setattr(pq, "_read_source", _fake_read)
    monkeypatch.setattr("time.sleep", lambda s: None)

    pairs = pq.crawl(["https://site.ru/category/x/"])
    assert {p["answer"] for p in pairs} == {"Париж", "Лев"}
    assert "https://site.ru/category/x/page/2/" in visited  # пагинация обойдена
