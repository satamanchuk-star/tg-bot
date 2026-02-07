# AlexBot

Telegram-бот для управления форумом жилого комплекса. Построен на Python 3.12 и aiogram 3.4.1.

## Возможности

- **Модерация**: страйки, мут, бан пользователей
- **Игры**: блэкджек с внутриигровой валютой (монеты)
- **Статистика**: отслеживание активности по топикам форума
- **Фильтрация**: автоматическая проверка на мат и флуд

## Архитектура

```
app/
├── main.py              # Точка входа, настройка бота и планировщика
├── config.py            # Конфигурация через переменные окружения
├── db.py                # Async SQLAlchemy + SQLite
├── models.py            # ORM-модели
├── handlers/            # Роутеры aiogram
│   ├── admin.py         # /mute, /ban, /strike, /addcoins
│   ├── games.py         # /21 — блэкджек
│   ├── moderation.py    # Фильтрация контента
│   └── ...
├── services/            # Бизнес-логика
│   ├── games.py         # Логика блэкджека, лидерборд
│   ├── strikes.py       # Управление страйками
│   └── ...
└── utils/               # Вспомогательные функции
```

**Ключевые принципы:**
- Полностью асинхронный код (async/await)
- Разделение на handlers (обработка команд) и services (бизнес-логика)
- Планировщик APScheduler для периодических задач (лидерборд, heartbeat)

## Конфигурация

Переменные окружения в `.env` (см. `.env.example`):

| Переменная | Описание |
|------------|----------|
| `BOT_TOKEN` | Токен бота от BotFather |
| `FORUM_CHAT_ID` | ID форума (supergroup) |
| `ADMIN_LOG_CHAT_ID` | Куда слать логи админу |
| `DATABASE_URL` | Строка подключения к БД (по умолчанию SQLite) |
| `TIMEZONE` | Часовой пояс (по умолчанию Europe/Moscow) |
| `BUILD_VERSION` | Версия сборки для логов (по умолчанию dev) |
| `TOPIC_*` | ID топиков форума (см. `.env.example`) |

## CI/CD

При пуше в `main` автоматически собирается и пушится Docker-образ (GitHub Actions).

Секреты в репозитории (Settings → Secrets → Actions):
- `DOCKERHUB_USERNAME` — логин Docker Hub
- `DOCKERHUB_TOKEN` — Access Token

## Деплой

Собрать образ локально (или дождаться CI):
```bash
# Linux/macOS
sh build.sh

# Windows
build.bat
```

На сервере:
```bash
cd /opt/alexbot

# Обновить приложение
sh reload.sh

# Посмотреть логи
docker compose logs
```

## Локальный запуск

```bash
# Установить зависимости
pip install -r requirements.txt

# Запустить бота
python -m app.main
```

## Викторина: загрузка вопросов

Команда `/load_quiz` загружает вопросы из файла `viktorinavopros_QA.xlsx` в корне проекта.

Требования к таблице:
- Лист `sheet1`.
- Колонка A — вопрос.
- Колонка B — ответ.
