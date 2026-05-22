# JetPing

Real API setup: [REAL_API.md](REAL_API.md)

Telegram-бот для проверки цен на авиабилеты.

Первый этап MVP: бот принимает маршрут и даты, получает текущую минимальную цену и отправляет результат пользователю. Автоматическое отслеживание, база подписок и уведомления по расписанию будут добавлены следующим этапом.

## Возможности первого этапа

- Команда `/price` для ручной проверки цены.
- Ввод маршрута IATA-кодами, например `MOW` -> `LED`.
- Ввод даты вылета и опциональной даты возвращения.
- Провайдер `mock` для локальной разработки без внешних API.
- Провайдер `travelpayouts` для Aviasales Data API через Travelpayouts.

## Быстрый запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполните `.env`:

```env
TELEGRAM_BOT_TOKEN=telegram_bot_token_from_botfather
JETPING_PRICE_PROVIDER=mock
```

Запуск:

```bash
python main.py
```

## Подключение Aviasales / Travelpayouts

Для реального источника цен укажите:

```env
JETPING_PRICE_PROVIDER=travelpayouts
TRAVELPAYOUTS_TOKEN=your_travelpayouts_token
JETPING_CURRENCY=rub
JETPING_MARKET=ru
```

Используется endpoint `https://api.travelpayouts.com/aviasales/v3/prices_for_dates`.

## Команды бота

- `/start` - приветствие.
- `/help` - подсказка по формату ввода.
- `/price` - проверить текущую цену.
- `/cancel` - отменить текущий ввод.

## Ограничения первого этапа

- Города вводятся IATA-кодами, а не свободным текстом.
- Проверка выполняется вручную по команде `/price`.
- Подписки и уведомления о снижении цены пока не сохраняются.
- SQLite и планировщик проверок будут добавлены на следующем этапе.

## План следующего этапа

- Добавить SQLite для хранения маршрутов пользователя.
- Добавить интервалы проверки: 30 минут, 1 час, 10 часов, 24 часа.
- Добавить фоновый scheduler.
- Отправлять уведомления только при снижении цены.
