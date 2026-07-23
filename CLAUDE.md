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
├── main.py              # Entry point, bot setup, APScheduler jobs
├── config.py            # Pydantic Settings (env vars)
├── db.py                # SQLAlchemy async setup
├── models.py            # ORM models (Strike, GameState, UserStat, etc.)
├── handlers/            # aiogram routers
│   ├── admin.py         # /mute, /ban, /strike, /addcoins, /reload_profanity
│   ├── blackjack.py     # /21 со ставками, /бонус, /score, /21top, /подарить + джобы
│   ├── moderation.py    # Content filtering, flood prevention
│   ├── forms.py         # FSM-based forms
│   ├── help.py          # Bot mention handling
│   └── stats.py         # Statistics endpoints
├── services/            # Business logic
│   ├── blackjack.py     # Логика «21», выплаты, GameRound-история
│   ├── coins.py         # Экономика монет: DEFAULT_COINS=200, бонус, спасение банкрота
│   ├── strikes.py       # Strike management
│   ├── flood.py         # Flood detection
│   ├── health.py        # Heartbeat monitoring
│   └── topic_stats.py   # Forum topic statistics
└── utils/               # Utilities (admin checks, text, time, profanity)
```

**Игра «21» и монеты (июль 2026):**
- Тема `topic_games`, окно 22:00–00:00 МСК; бот там молчит и не модерирует.
- Ставки 5/10/25/50: победа ×2, блэкджек (21 двумя картами) ×2.5 floor, ничья — возврат.
- Баланс (`UserStat.coins`) персистентен ВСЕГДА: `/reset_stats` — UPDATE к 200 (не DELETE),
  `/restart_jobs` и сброс возвращают активные ставки (`refund_active_bets`).
- История партий `game_rounds` — вечный аудит, никогда не чистится.
- `games_played` инкрементируется при ставке, `wins` — при развязке (не задваивать).
- Джобы: 21:55 — случайное приглашение соседей (`_INVITATIONS`), 00:00 — закрытие
  партий + топ-5 по монетам за вечер + чистка сообщений, сб 21:00 — недельный лидерборд.

**Викторина (июль 2026):**
- Та же тема `topic_games`, старт 20:00 МСК (не пересекается с блэкджеком).
- 15 вопросов, 45 сек; «первый верный забирает вопрос» (+15 🪙), победитель тура +100 🪙.
- Матч ответов `quiz.check_answer`: опечатки прощаем (леммы+Левенштейн), **числа/даты — точно**,
  лишние слова ок, альтернативы через «/». Это главная забота — старая версия тут сыпалась.
- Один driver-таск на сессию (единственный писатель переходов) + `quiz_watchdog` (1 мин)
  возобновляет тур после рестарта. Состояние — `QuizSession.state_json` (персистентно).
- Конвейер вопросов: XLSX владельца («Вопрос | Ответ», в ответе сначала короткий
  ответ, потом пояснение) → `scripts/import_quiz_xlsx.py` (разделяет ответ/пояснение,
  «(зачёт: …)» → альтернатива) → `data/quiz_questions.json` (~1800 шт.) →
  **валидация** (`scripts/validate_quiz.py`) → `scripts/seed_quiz.py` → игра.
  CI-гейт: `tests/test_quiz_questions_valid.py`.
- Пояснение (`QuizQuestion.comment`) показывается при развязке вопроса («💬 …»).
- **Вопросы НИКОГДА не повторяются** (recycle убран): когда свежих меньше, чем на
  тур, — одно уведомление владельцу+жителям, флаг `quiz_bank_exhausted`, викторина
  закрыта (и анонс 19:55 молчит). Пополнение базы через сид снимает флаг автоматически.
- Число↔слово в матче: «8» ⇄ «восемь» (`_canon_number`); годы/даты — только цифрами.
- Один driver-таск = единственный писатель переходов; финиш зовётся ТОЛЬКО вне лока
  (реентрантный `_lock_for` = дедлок); событие чистится при подготовке вопроса, не в driver.
- `start_quiz_auto` с `misfire_grace_time` (рестарт у 20:00 не теряет тур) и громким
  алертом в админ-чат при отказе. Watchdog (1 мин) возобновляет driver после рестарта.
- История — `quiz_rounds` (вечный аудит, all-time топ `/викторина_топ`).
- Джобы: 19:55 анонс (`announce_quiz_soon`), 20:00 старт, watchdog каждую минуту.

**Key patterns:**
- Async-first: async SQLAlchemy, async handlers, asyncio throughout
- Router-based handlers included in Dispatcher
- Service layer separates business logic from handlers
- Scheduled jobs via APScheduler (leaderboard, heartbeat, game timeouts)
- Forum topics tracked via config topic IDs

## Tech Stack

- **aiogram 3.4.1** - Telegram bot framework
- **SQLAlchemy 2.0.25** - Async ORM with aiosqlite
- **Pydantic Settings** - Configuration from environment
- **APScheduler** - Recurring jobs
- **anthropic SDK** - прямой доступ к Claude (Messages API); по умолчанию `claude-haiku-4-5`, премиум — `claude-sonnet-4-6`

## Данные о местах и знаниях (единый источник истины)

Чтобы адреса/графики не дублировались и не устаревали в двух местах:

- **Таблица `places`** (`data/places_seed.json` → БД) — АВТОРИТЕТ по «где / адрес /
  телефон / часы / расстояние» внешней инфраструктуры (магазины, аптеки, банки,
  ПВЗ, транспорт, АЗС и т.д.). У каждого места `verified_at`/`verified_by`.
  Поиск — `_get_places_context` (фильтрация в Python: SQLite LIKE не умеет
  кириллический регистр). Синонимы запросов — `_PLACES_SYNONYMS`.
- **`data/resident_kb.json`** — АВТОРИТЕТ по «как / правила / процедуры» (пропуск
  на шлагбаум, подача показаний, правила чата, контакты УК, аварийка). НЕ дублируй
  сюда volatile-адреса внешних мест — вместо этого короткий указатель («спроси
  «где аптека»»). Иначе устаревший адрес в KB перебьёт свежий из `places`
  (приоритет промпта: resident_canonical > rag > faq > places).
- Достоверность: кнопка «⚠️ Устарело» под ответами → лог-чат + персистентная
  запись (`log_stale_report`, видна в `/kb_stale` и недельном дайджесте,
  закрывается ответом админа в RAG) + инвалидация кэша ответов; 👎 тоже
  инвалидирует кэш и кладёт вопрос в «безответные». Вторничная авто-сверка
  `place_verify`; `/kb_stale` — отчёт о несвежих данных.
- Бюджет контекста знаний: все `<knowledge_base>`-блоки суммарно ≤ 4000
  символов (`_apply_kb_budget`, приоритет resident_canonical > rag > faq >
  places > web), RAG — top-3. Обращение к боту получает ответ даже без «?»
  (молчание = «бот игнорит»); спам «не знаю» ограничен только кулдауном.

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

## Git

- Не указывать Claude как соавтора в коммитах (без Co-Authored-By)
