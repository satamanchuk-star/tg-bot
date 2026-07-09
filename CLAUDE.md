# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AlexBot is a Telegram forum management bot for a residential community, built with Python 3.12 and aiogram 3.4.1. Features include moderation (strikes, muting, banning), gamification (blackjack with coins), topic statistics, and admin controls.

## Commands

```bash
# Run locally
python -m app.main

# Build and push Docker image
./build.sh      # Linux/macOS

# Deploy (pull and restart)
./reload.sh

# Docker operations
docker-compose up -d      # Start
docker-compose down       # Stop
docker-compose logs -f    # View logs
```

## Architecture

```
app/
├── main.py              # Entry point: init_db, миграции, APScheduler jobs, регистрация роутеров
├── config.py            # Pydantic Settings: env vars, topic IDs, мультимодельный AI-роутинг
├── db.py                # SQLAlchemy async engine/session
├── models.py            # ORM-модели (см. «Карта файлов» — их 25+)
├── handlers/            # aiogram-роутеры (порядок include в main.py важен!)
│   ├── admin.py         # /mute, /ban, /strike, /addcoins, /reload_profanity + inline-подтверждения
│   ├── moderation.py    # Контент-фильтр, антифлуд (catch-all, пропускает FSM)
│   ├── help.py          # Обработка упоминаний бота, AI-ответы (catch-all, не блокирует)
│   ├── games.py         # /21 blackjack (роутер ОТКЛЮЧЁН в main.py)
│   ├── shop.py          # Магазин монет (FSM)
│   ├── economy.py       # Инициативы жителей — доработки бота за монеты
│   ├── forms.py         # FSM-анкеты (шлагбаум, соседи)
│   ├── suggest.py       # Предложить место инфраструктуры ЖК
│   ├── text_publish.py  # /text — публикация текста от лица бота в топик
│   ├── personalization.py # /off_nudges, /on_nudges (только DM)
│   └── welcome.py       # Автоприветствие новичков (роутер НЕ подключён)
├── services/            # Бизнес-логика (см. «Карта файлов»)
│   ├── ai_*.py          # AI-слой: ai_module, ai_router, ai_tasks, ai_schemas, ai_usage
│   ├── rag.py, resident_kb.py, resident_profile.py  # Знания и память
│   ├── games.py, shop.py, strikes.py, flood.py       # Геймплей и модерация
│   └── ...              # mood, learning, proactive, health, topic_stats, sheets и др.
└── utils/               # admin (проверки прав), text, time, profanity, safe_telegram, admin_help
```

**Key patterns:**
- Async-first: async SQLAlchemy, async handlers, asyncio throughout
- Router-based handlers included in Dispatcher (**порядок важен** — FSM-роутеры до `moderation`)
- Service layer separates business logic from handlers
- Scheduled jobs via APScheduler (leaderboard, heartbeat, game timeouts, daily summary)
- Forum topics tracked via config topic IDs (`topic_*` в `config.py`)
- Мультимодельный AI-роутинг: задача → модель/температура/токены (`services/ai_router.py`)

## Tech Stack

- **aiogram 3.4.1** - Telegram bot framework
- **SQLAlchemy 2.0.25** - Async ORM with aiosqlite
- **Pydantic Settings** - Configuration from environment
- **APScheduler** - Recurring jobs
- **anthropic SDK** - прямой доступ к Claude (Messages API); по умолчанию `claude-haiku-4-5`, премиум — `claude-sonnet-4-6`

## Configuration

Environment variables in `.env`:
- `BOT_TOKEN` - Telegram bot token (required)
- `ANTHROPIC_API_KEY` - ключ Anthropic Claude (алиасы: `AI_KEY`, `AI_API_KEY`)
- `FORUM_CHAT_ID` - Forum supergroup ID
- `ADMIN_LOG_CHAT_ID` - Admin notifications destination
- `DATABASE_URL` - Database connection (default: SQLite)
- `TIMEZONE` - Default Europe/Moscow
- Topic IDs for forum sections

## Безопасность (ОБЯЗАТЕЛЬНО)

- **Никаких паролей/токенов/ключей в открытом виде в репозитории.** Только ссылки
  `${{ secrets.* }}` в workflow. `.env` не коммитится; `.env.example` — пустые поля.
- Секреты сервера хранятся **только** в GitHub Secrets. Всё содержимое env-файла —
  в одном секрете `BOT_ENV`; деплой пишет его в `/opt/alexbot/.env`. В
  `docker-compose.yaml` секретов нет.
- Не добавляй парсинг секретов из файлов на сервере (старый
  `_inject_env_from_server_compose` удалён намеренно).

## Code Style

- Comments in Russian
- Python type hints throughout
- No formal linting configured
- Проверка синтаксиса: `python3 -m py_compile <file>`

---

## Правила разработки (ОБЯЗАТЕЛЬНО)

Единый протокол для любой правки. Цель — производственный результат: рабочий,
минимально сложный код с тестами.

### Рабочий цикл
1. **Понять контекст.** Прочитать релевантные файлы по «Карте файлов» ниже,
   прежде чем писать код. Не выдумывать API — проверять сигнатуры на месте.
2. **Спланировать.** Сформулировать минимальную версию (V1), разбить на шаги
   по 1–3 файла. Для крупных задач — короткий план + список файлов заранее.
3. **Реализовать** один законченный шаг.
4. **Тесты.** Добавить/обновить тесты в `tests/` под изменённую логику.
5. **Проверить.** `python3 -m py_compile <изменённые файлы>` + `pytest` по
   затронутым тестам. Указывать команды запуска.
6. **Самопроверка.** Перечислить краевые случаи и риски.

### Инварианты, которые нельзя нарушать
- **Async-first.** Никаких блокирующих вызовов в хендлерах/джобах. Внешние
  вызовы (Telegram API, БД, AI, Sheets) — только `await`, оборачивать в
  `safe_telegram`/try-except там, где падение прерывает джобу.
- **Слои.** Бизнес-логика — в `services/`, хендлеры — тонкие. Не дублировать
  логику между хендлерами; выносить в сервис.
- **Порядок роутеров.** FSM-роутеры (`forms`, `shop`) регистрируются **до**
  `moderation` (catch-all). Меняя порядок в `main.py`, проверять, что FSM не
  перехватывается модерацией.
- **Права.** Любая админ-команда проверяет доступ через `utils/admin.py`
  (`is_admin` / `is_admin_message`). При ошибке проверки — **отказ**, не доступ.
- **AI-задачи.** Новую AI-задачу заводить через `services/ai_router.py`
  (модель/температура/лимит токенов) + `ai_tasks.py`, а не хардкодить модель.
  Учитывать квоты через `ai_usage.py`.
- **Миграции БД.** Новая таблица/поле → модель в `models.py` + идемпотентная
  миграция в `init_db`/`validate_db` (`main.py`), совместимая с SQLite.
- **Зависимости.** Не добавлять пакеты без необходимости; при добавлении —
  править `requirements.txt` и проверять сборку Docker.
- **Секреты.** См. раздел «Безопасность» — никаких секретов в репозитории.

### Definition of Done
Код компилируется, тесты проходят, права проверяются, секреты не утекли,
изменение задокументировано в PR, стиль соблюдён (типы, русские комментарии).

---

## Роли разработки

Два взаимодополняющих понимания «ролей»: рабочие роли процесса (что делается на
каждом этапе) и роли-субагенты Claude (специализированные режимы, вызываемые
через Agent tool).

### Роли процесса
| Роль | Ответственность | Артефакт |
|------|-----------------|----------|
| **Архитектор** | Проектирует изменение до кода: точки входа, слои, влияние на БД/AI/деплой | План + список файлов |
| **Инженер** | Реализует шаг, пишет минимально сложный код, соблюдает инварианты | Код + тесты |
| **Ревьюер** | Проверяет корректность, слои, права, безопасность, стиль | Замечания/аппрув |
| **QA** | Гоняет тесты и краевые случаи, проверяет запуск | Отчёт о проверке |
| **Security** | Следит за секретами, правами, деплоем через GitHub Secrets | Аудит безопасности |

### Роли-субагенты Claude (`.claude/agents/`)
Специализированные агенты, которые можно вызвать через Agent tool. Каждый — с
собственным системным промптом под контекст этого репозитория:
- **architect** — планирование изменений (read-only, без правок кода).
- **code-reviewer** — ревью текущего диффа на корректность/слои/права/стиль.
- **bugfixer** — диагностика и точечное исправление багов с воспроизведением.
- **security-auditor** — проверка правил безопасности (секреты, `.env`, деплой).

---

## Роли пользователей бота

Роли в самом продукте (не в разработке). Определяют доступ к командам и логике.

| Роль | Как определяется | Доступ |
|------|------------------|--------|
| **Creator / Administrator** | `utils/admin.is_admin` — статус в чате (`creator`/`administrator`), включая анонимных админов | Все админ-команды (`/mute`, `/ban`, `/strike`, `/addcoins`, `/text`, `/reload_profanity`), правки ответов бота, сброс статистики |
| **Житель (обычный участник)** | Любой участник форума | Игры (`/21` — временно отключены), магазин, экономика/доработки за монеты, анкеты, предложить место, персональные нажъмы (DM), обычные сообщения с AI-ответами |
| **Бот** | `message.from_user.is_bot` / собственный ID | Публикация от лица бота, реакции, приветствия, проактивные подсказки |
| **Лог-чат (модераторы)** | `ADMIN_LOG_CHAT_ID` | Получает уведомления модерации и коррекции знаний на подтверждение (кнопки Принять/Отклонить) |

**Правило доступа:** проверка прав — только через `utils/admin.py`. Никогда не
доверять `user_id` из сообщения как признаку админа без проверки статуса в чате.

---

## Карта файлов: что трогать под типовую задачу

Практический индекс «хочу X → смотри/меняй файлы Y». Перед правкой прочитать
перечисленные файлы.

| Задача | Файлы |
|--------|-------|
| **Новая админ-команда** | `handlers/admin.py` (+ проверка `utils/admin.py`); регистрация команд в `main.py` (`_set_admin_commands`); при новой бизнес-логике — сервис в `services/` |
| **Новая пользовательская команда** | новый/существующий `handlers/*.py`; `dp.include_router(...)` в `main.py` (учесть порядок!); публичные команды — `_set_public_commands` |
| **Модерация / фильтр контента** | `handlers/moderation.py`, `services/flood.py`, `utils/text.py`, `utils/profanity.py`; самообучение/коррекции — `services/learning.py`, `services/admin_corrections.py` |
| **Игры / блэкджек** | `handlers/games.py` (UI/callbacks, роутер отключён — включить в `main.py` при возврате фичи), `services/games.py` (логика, рейтинг); таймауты — джоба `check_game_timeouts` в `main.py` |
| **Экономика / монеты / магазин** | `handlers/shop.py` + `services/shop.py`; доработки бота — `handlers/economy.py` + `services/improvements.py`; начисление — `/addcoins` в `admin.py`, модель `UserStat` |
| **AI-ответы / промпты** | `services/ai_module.py` (генерация), `services/ai_tasks.py` (задачи), `services/ai_router.py` (модель/темп/токены), `services/ai_schemas.py` (структурированный вывод); контекст — `chat_history.py`, `mood.py` |
| **Новая AI-задача** | добавить запись в `_TASK_CONFIG` (`ai_router.py`) + соответствующие `ai_*_model` поля в `config.py`; вызов через `ai_tasks.py`; квоты — `ai_usage.py` |
| **База знаний / RAG / память** | `services/rag.py`, `services/resident_kb.py` (перечитать без рестарта — `/kb_reload`), `services/resident_profile.py`, `services/faq.py`; веб-поиск — `services/web_search.py` |
| **Новая модель БД / поле** | `models.py` (ORM) + идемпотентная миграция в `init_db`/`validate_db` (`main.py`); обслуживание размера — `services/db_maintenance.py` |
| **Периодическая задача (cron)** | `schedule_jobs` в `main.py` (`scheduler.add_job(...)`); саму логику — в сервис (`daily_messages.py`, `topic_stats.py`, `health.py`, `proactive.py`) |
| **Статистика / сводки** | сбор — `LoggingMiddleware` в `main.py`, `services/topic_stats.py`, `services/daily_messages.py`; сброс — `services/admin_stats_reset.py` |
| **Приветствие / онбординг** | `handlers/welcome.py` (роутер не подключён), `handlers/forms.py`; топики — поля `topic_*` в `config.py` |
| **Топики форума** | поля `topic_*` в `config.py` (+ валидатор списка); использование — по всему коду через `settings.topic_*` |
| **Google Sheets / места** | `services/sheets.py`, `handlers/suggest.py`, модель `Place`; импорт — `scripts/` |
| **Конфиг / env-переменная** | `config.py` (Pydantic Settings) + `.env.example` (пустое поле); **секрет — только в GitHub Secrets** |
| **Здоровье / heartbeat** | `services/health.py`; джоба `heartbeat_job` в `main.py` |
| **Устойчивость к ошибкам Telegram** | `utils/safe_telegram.py` — оборачивать вызовы API, которые не должны ронять джобу |
| **Тесты** | `tests/` — зеркалит структуру; фикстуры в `tests/conftest.py`; запуск: `pytest tests/<файл>` |
| **Деплой / CI** | `.github/workflows/build.yml`, `quality-gate.yml`, `Dockerfile`, `docker-compose.yaml`, `reload.sh` |

---

## Git

- Не указывать Claude как соавтора в коммитах (без Co-Authored-By)
