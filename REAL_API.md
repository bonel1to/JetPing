# JetPing: real price source

This project uses the Aviasales Data API through Travelpayouts for real flight prices.

Important limitation: this API returns cached Aviasales data, not guaranteed live airline inventory. Travelpayouts documentation says the cache is based on user searches and can contain prices found during the last 48 hours; cached data is stored for up to 7 days.

Official docs:

- Aviasales Data API: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API
- API limits: https://support.travelpayouts.com/hc/en-us/articles/4402565416594-API-rate-limits

## 1. Get a Travelpayouts API token

1. Create or open a Travelpayouts account.
2. Go to the developer/API key section.
3. Copy your API token.

## 2. Configure `.env`

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
JETPING_PRICE_PROVIDER=travelpayouts
TRAVELPAYOUTS_TOKEN=your_travelpayouts_token
JETPING_CURRENCY=rub
JETPING_MARKET=ru
```

Keep `.env` private. Do not commit it to GitHub.

## 3. Check the API without Telegram

Use this command from the project folder:

```powershell
python -m app.check_price MOW LED 2026-06-20
```

Round trip example:

```powershell
python -m app.check_price MOW AER 2026-07-01 --return-date 2026-07-10
```

If the command returns `No tickets found`, try a popular route and a wider/nearer date. The Travelpayouts endpoint works from cached search data, so not every exact route/date pair has a result.

## 4. Run the bot

```powershell
python main.py
```

Then send `/price` to the bot in Telegram.
