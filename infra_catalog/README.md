# Инфраструктурный каталог — ЖК «Живописный»

ETL-пайплайн для сбора, нормализации, дедупликации и экспорта инфраструктурных объектов в радиусе 10 км от ЖК «Живописный» (Ленинский городской округ, Московская область).

## Структура проекта

```
infra_catalog/
├── main.py              # Точка входа, CLI, ETL-пайплайн
├── __main__.py           # python -m infra_catalog
├── config.py             # Конфигурация (координаты, пути, пороги)
├── constants.py          # Справочник category/subcategory
├── logging_config.py     # Настройка логирования
├── requirements.txt      # Зависимости
│
├── models/
│   └── schema.py         # Pydantic-модели: RawObject, InfraObject, ValidationIssue
│
├── core/
│   ├── geo.py            # Haversine, проверка радиуса
│   ├── normalizers.py    # Нормализация: телефоны, адреса, названия, URL, dedup-ключ
│   ├── classifier.py     # Классификация по category/subcategory (keyword rules)
│   ├── validators.py     # Валидация объектов
│   ├── deduplicator.py   # Дедупликация по ключу + proximity + fuzzy name
│   └── merger.py         # Слияние дублей с учётом приоритета источников
│
├── providers/
│   ├── base.py           # BaseProvider (абстрактный интерфейс)
│   ├── static_loader.py  # Загрузка из JSON/CSV файлов
│   ├── yandex_maps.py    # Заглушка — Яндекс.Карты API
│   ├── google_maps.py    # Заглушка — Google Maps API
│   ├── gis2.py           # Заглушка — 2GIS API
│   ├── official_sites.py # Заглушка — парсеры официальных сайтов
│   └── regional_portals.py # Заглушка — региональные порталы
│
├── exporters/
│   ├── csv_exporter.py   # CSV + validation_issues.csv
│   ├── json_exporter.py  # JSON + category_dictionary.json
│   ├── xlsx_exporter.py  # XLSX (objects, category_dictionary, meta)
│   └── sql_exporter.py   # SQL seed INSERT-скрипт
│
├── sql/
│   └── schema.sql        # DDL для PostgreSQL
│
├── data/
│   ├── raw/              # Входные данные (JSON/CSV от провайдеров)
│   ├── processed/        # Результаты: objects.csv, objects.json, objects.xlsx, seed.sql
│   └── reports/          # validation_issues.csv
│
└── tests/
    ├── test_geo.py
    ├── test_normalizers.py
    ├── test_classifier.py
    └── test_deduplicator.py
```

## Установка

```bash
pip install -r infra_catalog/requirements.txt
```

## Запуск

```bash
# С настройками по умолчанию (demo-данные)
python -m infra_catalog

# С параметрами
python -m infra_catalog \
  --center-lat 55.525238 \
  --center-lon 37.616287 \
  --radius-km 10.0 \
  --input-dir infra_catalog/data/raw \
  --output-dir infra_catalog/data/processed \
  --providers static \
  --verbose
```

### CLI-аргументы

| Аргумент | По умолчанию | Описание |
|---|---|---|
| `--center-lat` | 55.525238 | Широта центральной точки |
| `--center-lon` | 37.616287 | Долгота центральной точки |
| `--radius-km` | 10.0 | Радиус поиска (км) |
| `--input-dir` | `data/raw/` | Директория с входными файлами |
| `--output-dir` | `data/processed/` | Директория для результатов |
| `--providers` | все | Список провайдеров через пробел |
| `--verbose` / `-v` | false | Подробное логирование |

## Входные данные

Провайдер `StaticLoaderProvider` принимает JSON и CSV файлы из `--input-dir`.

### JSON формат

```json
[
  {
    "source_name": "yandex_maps",
    "raw_name": "Поликлиника №2",
    "raw_type": "поликлиника",
    "raw_address": "г. Видное, ул. Заводская, д. 17",
    "raw_phone": "8 (495) 541-11-22",
    "raw_website": "vidnoe-poliklinika2.ru",
    "raw_work_time": "Пн-Пт 8:00-20:00",
    "raw_lat": 55.5527,
    "raw_lon": 37.6105,
    "raw_category": "",
    "raw_subcategory": ""
  }
]
```

Также поддерживается формат `{"objects": [...]}`.

### CSV формат

Колонки: `source_name`, `raw_name`, `raw_type`, `raw_address`, `raw_phone`, `raw_website`, `raw_work_time`, `raw_description`, `raw_lat`, `raw_lon`, `raw_category`, `raw_subcategory`.

Допускаются и упрощённые имена: `name`, `type`, `address`, `phone`, `website`, `lat`, `lon`, `category`, `subcategory`.

## Выходные файлы

| Файл | Описание |
|---|---|
| `data/processed/objects.csv` | Основная таблица (UTF-8, CSV) |
| `data/processed/objects.json` | Основная таблица (JSON) |
| `data/processed/objects.xlsx` | XLSX: листы objects, category_dictionary, meta |
| `data/processed/category_dictionary.json` | Справочник категорий |
| `data/processed/seed.sql` | SQL INSERT-скрипт для PostgreSQL |
| `data/reports/validation_issues.csv` | Лог проблем, отбросов и слияний |

## Дедупликация

Объект считается дублем, если:
1. Совпадает нормализованный ключ `name + address` (нормализация: lower, trim, ё→е, удаление кавычек, унификация сокращений адреса)
2. ИЛИ: объекты одной категории находятся в пределах 100м друг от друга и сходство названий ≥ 80% (rapidfuzz)

При слиянии:
- Выбирается значение от более приоритетного источника
- Телефоны и описания объединяются
- Источники объединяются через "; "

## Категории

Фиксированный справочник: `medical`, `food`, `government`, `post`, `mfc`, `education`, `mall`, `grocery`, `building_materials`. Подкатегории см. в `constants.py` и `category_dictionary.json`.

## Провайдеры

Реализованы:
- **StaticLoaderProvider** — загрузка из JSON/CSV

Заглушки (готовы к реализации при наличии API-ключей):
- YandexMapsProvider
- GoogleMapsProvider
- Gis2Provider
- OfficialSitesProvider
- RegionalPortalsProvider

Для подключения нового провайдера: унаследовать `BaseProvider`, реализовать метод `fetch()`, добавить в `build_providers()` в `main.py`.

## Тесты

```bash
cd /path/to/tg-bot
python -m pytest infra_catalog/tests/ -v
```

## Текущие ограничения

- Реальные API-провайдеры (Яндекс, Google, 2GIS) не реализованы — нужны API-ключи
- Геокодирование адресов не реализовано (интерфейс готов для подключения)
- Классификатор работает по keyword-правилам — покрывает основные случаи, но может требовать дополнения для редких типов
- Демо-данные показывают работоспособность пайплайна, но не являются реальной выгрузкой

## Интеграция с Telegram-ботом

Экспортные файлы (CSV, JSON) и SQL-схема готовы для загрузки в БД бота. Модуль `infra_catalog` можно импортировать из кода бота:

```python
from infra_catalog.models import InfraObject
from infra_catalog.main import run_pipeline
```
