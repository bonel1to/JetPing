# JetPing

Telegram-бот для проверки цен на авиабилеты по маршруту и датам.

Текущая версия умеет принимать параметры поиска через Telegram, получать цену из Aviasales Data API через Travelpayouts и сохранять историю успешных поисков в локальную SQLite-базу.

Подробно про реальный API: [REAL_API.md](REAL_API.md)  
Подробно про БД: [database.md](database.md)

## Возможности

- Команда `/price` для ручной проверки цены.
- Ввод маршрута IATA-кодами, например `MOW` -> `LED`.
- Ввод даты вылета и опциональной даты возвращения.
- Один рабочий источник реальных цен: `travelpayouts`.
- Тестовый источник `mock` для локальной разработки без внешнего API.
- Сохранение успешных поисков в SQLite.
- CLI-проверка источника цен без запуска Telegram.

## Стек

- Python
- python-telegram-bot
- requests
- python-dotenv
- SQLite через стандартный модуль `sqlite3`

## Быстрый запуск

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Если PowerShell блокирует активацию `.venv`, можно запускать Python напрямую:

```powershell
.\.venv\Scripts\python.exe main.py
```

## Конфигурация

Минимальная локальная конфигурация с тестовым провайдером:

```env
TELEGRAM_BOT_TOKEN=telegram_bot_token_from_botfather
JETPING_PRICE_PROVIDER=mock
JETPING_DATABASE_PATH=data/jetping.db
```

Конфигурация с реальным Travelpayouts / Aviasales API:

```env
TELEGRAM_BOT_TOKEN=telegram_bot_token_from_botfather
JETPING_PRICE_PROVIDER=travelpayouts
TRAVELPAYOUTS_TOKEN=your_travelpayouts_token
JETPING_CURRENCY=rub
JETPING_MARKET=ru
JETPING_DATABASE_PATH=data/jetping.db
```

Файл `.env` содержит секреты и не должен попадать в Git.

## Запуск бота

```powershell
python main.py
```

После запуска откройте бота в Telegram и отправьте:

```text
/start
```

Для проверки цены используйте:

```text
/price
```

Бот последовательно запросит:

1. IATA-код города вылета.
2. IATA-код города прилета.
3. Дату вылета в формате `YYYY-MM-DD`.
4. Дату возвращения или `-` для билета в одну сторону.

## Команды бота

- `/start` - приветствие.
- `/help` - подсказка по формату ввода.
- `/price` - проверить текущую цену.
- `/cancel` - отменить текущий ввод.

## Проверка API без Telegram

Для диагностики источника цен можно использовать CLI-команду:

```powershell
python -m app.check_price MOW LED 2026-06-20
```

Пример для билета туда-обратно:

```powershell
python -m app.check_price MOW AER 2026-07-01 --return-date 2026-07-10
```

Если используется `JETPING_PRICE_PROVIDER=travelpayouts`, команда обращается к реальному API. Если используется `mock`, возвращается детерминированная тестовая цена.

## База данных

В проекте используется SQLite. Путь к базе задается переменной:

```env
JETPING_DATABASE_PATH=data/jetping.db
```

При запуске приложение автоматически создает директорию `data/` и таблицу `searches`, если они отсутствуют.

В таблицу сохраняются успешные поиски:

- Telegram ID пользователя;
- маршрут;
- даты;
- сервис-источник;
- найденная цена;
- валюта;
- авиакомпания, если есть;
- ссылка на билет, если есть;
- timestamp сохранения.

Файл базы данных локальный и игнорируется Git через `.gitignore`.

## Структура проекта

```text
JetPing/
├── app/
│   ├── __init__.py
│   ├── bot.py
│   ├── check_price.py
│   ├── config.py
│   ├── db.py
│   └── price_provider.py
├── .env.example
├── .gitignore
├── database.md
├── main.py
├── README.md
├── REAL_API.md
└── requirements.txt
```

## Проверка перед коммитом

```powershell
python -B -m py_compile app\__init__.py app\bot.py app\config.py app\price_provider.py app\check_price.py app\db.py main.py
python -B -m app.check_price MOW LED 2026-06-20
```

## Ограничения текущей версии

- Проверка цены выполняется вручную по команде `/price`.
- Автоматические подписки и уведомления о снижении цены пока не реализованы.
- Города вводятся IATA-кодами, а не обычными названиями.
- Основной реальный источник один: Travelpayouts / Aviasales.
- Travelpayouts Data API возвращает кешированные данные, поэтому не для каждого маршрута и даты есть результат.

## План следующего этапа

- Добавить команду `/track` для сохранения маршрута на отслеживание.
- Добавить `/list` для просмотра активных отслеживаний.
- Добавить `/delete` для удаления отслеживания.
- Добавить интервалы проверки: 30 минут, 1 час, 10 часов, 24 часа.
- Добавить фоновый scheduler.
- Отправлять уведомления только при снижении цены.
