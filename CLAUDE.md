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
│   ├── games.py         # /21 blackjack command and callbacks
│   ├── moderation.py    # Content filtering, flood prevention
│   ├── forms.py         # FSM-based forms
│   ├── help.py          # Bot mention handling
│   └── stats.py         # Statistics endpoints
├── services/            # Business logic
│   ├── games.py         # Blackjack logic, leaderboard
│   ├── strikes.py       # Strike management
│   ├── flood.py         # Flood detection
│   ├── health.py        # Heartbeat monitoring
│   └── topic_stats.py   # Forum topic statistics
└── utils/               # Utilities (admin checks, text, time, profanity)
```

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
