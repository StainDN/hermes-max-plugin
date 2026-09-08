# MAX Messenger — адаптер для Hermes Agent

Плагин подключает бота [MAX Messenger](https://max.ru/) к Hermes Agent через библиотеку [`maxapi`](https://github.com/love-apples/maxapi).

## Возможности

- Входящие текстовые сообщения и команды (`/start`, `/new`, `/model` и др.).
- Входящие медиафайлы: изображения, видео, аудио, документы, стикеры.
- Исходящие текстовые ответы с автоматическим разбиением длинных сообщений.
- Отправка медиа: картинки, документы, голосовые/аудио, видео.
- Индикатор набора текста (`SenderAction.TYPING_ON`).
- Inline-кнопки для `clarify`, подтверждения exec и slash-команд.
- Allowlist по user ID через `MAX_ALLOWED_USERS` / `MAX_ALLOW_ALL_USERS`.
- Доставка cron-уведомлений через `MAX_HOME_CHANNEL`.
- Standalone-отправитель для `deliver=max` cron-задач.

## Требования

- Hermes Agent
- Python 3.11+
- `maxapi==1.2.1`

## Установка

### 1. Добавь зависимость

В `pyproject.toml` проекта Hermes в группу `[project.optional-dependencies]` (обычно секция `messaging`) добавь:

```toml
[project.optional-dependencies]
messaging = [
    # ... другие адаптеры ...
    "maxapi==1.2.1",
]
```

Затем переустанови зависимости:

```bash
cd /opt/hermes
uv pip install -e ".[messaging]"
# или
pip install -e ".[messaging]"
```

### 2. Скопируй плагин

Распакуй содержимое архива в каталог плагинов Hermes:

```bash
unzip max-platform.zip -d /opt/hermes/plugins/platforms/
```

Должна получиться структура:

```
/opt/hermes/plugins/platforms/max/
├── __init__.py
├── adapter.py
├── plugin.yaml
├── README.md
└── test_adapter.py
```

### 3. Настрой окружение

В `~/.hermes/.env` (или `/opt/data/.env` в Docker-контейнере) добавь:

```bash
MAX_BOT_TOKEN=your_bot_token_here
MAX_ALLOWED_USERS=123456789,987654321
```

Переменные:

| Переменная | Обязательная | Описание |
|------------|--------------|----------|
| `MAX_BOT_TOKEN` | да | Токен бота из MAX Bot API |
| `MAX_ALLOWED_USERS` | нет | Разрешённые user ID через запятую |
| `MAX_ALLOW_ALL_USERS` | нет | `true`/`1` — разрешить всех (только для разработки) |
| `MAX_HOME_CHANNEL` | нет | Chat/user ID для cron-уведомлений |
| `MAX_HOME_CHANNEL_NAME` | нет | Отображаемое имя home-канала |
| `MAX_POLL_TIMEOUT` | нет | Таймаут long polling в секундах (по умолчанию 30) |
| `MAX_RECONNECT_BACKOFF` | нет | Задержки переподключения через запятую (по умолчанию `2,5,10,30,60`) |
| `MAX_API_URL` | нет | Кастомный URL API MAX |

### 4. Запусти gateway

```bash
hermes gateway start
# или в foreground
hermes gateway run
```

Проверь статус:

```bash
hermes gateway status
```

Должно появиться:

```text
✓ max connected
```

## Безопасность

Адаптер по умолчанию работает в режиме **deny**: если не задан `MAX_ALLOWED_USERS` и не включён `MAX_ALLOW_ALL_USERS`, бот не отвечает никому.

Рекомендуется явно указывать разрешённые user ID:

```bash
MAX_ALLOWED_USERS=123456789
```

Не используй `MAX_ALLOW_ALL_USERS=true` в продакшене.

## Тесты

```bash
cd /opt/hermes
/opt/hermes/.venv/bin/python -m pytest plugins/platforms/max/test_adapter.py -q
```

## Примечания

- Адаптер использует **long polling**. Webhook не реализован.
- Входящие сообщения дедуплицируются по message id.
- Собственные сообщения бота фильтруются, чтобы избежать эхо.
- Медиафайлы кешируются в каталогах Hermes для работы vision/tools.
