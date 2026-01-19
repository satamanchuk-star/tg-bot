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
build.bat       # Windows

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

## Configuration

Environment variables in `.env`:
- `BOT_TOKEN` - Telegram bot token (required)
- `FORUM_CHAT_ID` - Forum supergroup ID
- `ADMIN_LOG_CHAT_ID` - Admin notifications destination
- `DATABASE_URL` - Database connection (default: SQLite)
- `TIMEZONE` - Default Europe/Moscow
- Topic IDs for forum sections

## Code Style

- Comments in Russian
- Python type hints throughout
- No formal linting configured

## Git

- Не указывать Claude как соавтора в коммитах (без Co-Authored-By)
