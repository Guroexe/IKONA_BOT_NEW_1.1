# IKONA Telegram Bot

## Локальный запуск

1. Python **3.12** (см. `runtime.txt`).
2. `pip install -r requirements.txt`
3. Скопируйте `.env.example` → `.env`, заполните переменные.
4. Положите `credentials.json` (сервисный аккаунт Google с доступом к таблице) в корень проекта **или** задайте `GOOGLE_CREDENTIALS_JSON` в `.env` (одной строкой JSON).
5. Папка `gifs/new/` с GIF из кода (`privet_1.gif` и др.) — без неё бот отправит текст вместо анимации.
6. Запуск: `python main.py`

## Railway

1. Подключите этот репозиторий в Railway.
2. **Start command:** `python main.py`
3. **Variables** (минимум):

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `ADMIN_CHAT_ID` | id чата для уведомлений (целое число, может быть отрицательным для групп) |
| `GOOGLE_SHEET_ID` | id Google Spreadsheet |
| `GOOGLE_CREDENTIALS_JSON` | весь JSON сервисного аккаунта (одна переменная) |
| `POLZA_IKONA_CHAT_API_KEY` | ключ Polza для IKONA AI |

4. Рекомендуется **Volume** на корень приложения, чтобы сохранялись `bot_persistence.pickle` и `casino_win_feed.json`.
5. Один деплой на токен: второй процесс с тем же токеном даёт ошибку `409 Conflict`.

## Безопасность

Секреты не хранятся в коде: задаются через переменные окружения или `.env` (только локально). После публикации репозитория смените токены и ключи, которые раньше попадали в логи или старые коммиты.
