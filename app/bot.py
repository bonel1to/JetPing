from __future__ import annotations

import logging
from asyncio import gather, to_thread
from datetime import date

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.db import save_search
from app.price_provider import FlightSearch, PriceProvider, PriceProviderError, build_price_provider

LOGGER = logging.getLogger(__name__)

ORIGIN, DESTINATION, DEPARTURE_DATE, RETURN_DATE = range(4)


def create_application(settings: Settings) -> Application:
    providers = []
    for provider_name in settings.price_providers:
        providers.append(
            build_price_provider(
                name=provider_name,
                token=settings.travelpayouts_token,
                currency=settings.currency,
                market=settings.market,
            )
        )

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["price_providers"] = providers
    application.bot_data["database_path"] = settings.database_path
    application.bot_data["admin_ids"] = settings.admin_ids

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("price", price_start)],
            states={
                ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, origin_received)],
                DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, destination_received)],
                DEPARTURE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, departure_date_received)],
                RETURN_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, return_date_received)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    admin_ids = context.bot_data.get("admin_ids", set())
    greeting = "Привет, суперпользователь 👑!\n\n" if user_id in admin_ids else ""

    await update.message.reply_text(
        f"{greeting}JetPing отслеживает цены на авиабилеты.\n\n"
        "Первый этап: проверка текущей минимальной цены.\n"
        "Запусти /price и введи маршрут в IATA-кодах, например MOW -> LED."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/price - проверить текущую цену\n"
        "/cancel - отменить ввод\n\n"
        "Формат даты: YYYY-MM-DD.\n"
        "Города пока вводятся IATA-кодами: MOW, LED, AER, KZN."
    )


async def price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Введите город вылета IATA-кодом, например MOW:")
    return ORIGIN


async def origin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await update.message.reply_text("Введите IATA-код из 3 букв, например MOW:")
        return ORIGIN

    context.user_data["origin"] = code
    await update.message.reply_text("Введите город прилета IATA-кодом, например LED:")
    return DESTINATION


async def destination_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await update.message.reply_text("Введите IATA-код из 3 букв, например LED:")
        return DESTINATION

    if code == context.user_data.get("origin"):
        await update.message.reply_text("Город прилета должен отличаться от города вылета:")
        return DESTINATION

    context.user_data["destination"] = code
    await update.message.reply_text("Введите дату вылета в формате YYYY-MM-DD:")
    return DEPARTURE_DATE


async def departure_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed_date = parse_iso_date(update.message.text)
    if parsed_date is None:
        await update.message.reply_text("Не понял дату. Введите в формате YYYY-MM-DD:")
        return DEPARTURE_DATE

    if parsed_date < date.today():
        await update.message.reply_text("Дата вылета не может быть в прошлом. Введите будущую дату:")
        return DEPARTURE_DATE

    context.user_data["departure_date"] = parsed_date
    await update.message.reply_text(
        "Введите дату возвращения в формате YYYY-MM-DD или отправьте '-' для билета в одну сторону:"
    )
    return RETURN_DATE


async def return_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    return_date = None

    if text != "-":
        return_date = parse_iso_date(text)
        if return_date is None:
            await update.message.reply_text("Не понял дату. Введите YYYY-MM-DD или '-' для билета в одну сторону:")
            return RETURN_DATE

        if return_date < context.user_data["departure_date"]:
            await update.message.reply_text("Дата возвращения не может быть раньше даты вылета:")
            return RETURN_DATE

    search = FlightSearch(
        origin=context.user_data["origin"],
        destination=context.user_data["destination"],
        departure_date=context.user_data["departure_date"],
        return_date=return_date,
    )

    await update.message.reply_text("Проверяю цены у всех доступных провайдеров...")

    providers: list[PriceProvider] = context.bot_data["price_providers"]

    async def fetch_quote(provider: PriceProvider):
        try:
            return await to_thread(provider.get_lowest_price, search)
        except PriceProviderError as exc:
            LOGGER.warning("Provider %s failed: %s", provider.__class__.__name__, exc)
            return exc
        except Exception as exc:
            LOGGER.exception("Unexpected error in provider %s", provider.__class__.__name__)
            return exc

    results = await gather(*(fetch_quote(p) for p in providers))
    valid_quotes = [q for q in results if not isinstance(q, Exception) and q is not None]
    failed_count = sum(1 for q in results if isinstance(q, Exception))

    if not valid_quotes:
        await update.message.reply_text("Не удалось получить цену ни в одном из сервисов.")
        return ConversationHandler.END

    # Sort quotes by price ascending
    valid_quotes.sort(key=lambda q: q.price)

    # Save all successful searches to the database
    try:
        user_id = update.effective_user.id
        database_path = context.bot_data["database_path"]
        for q in valid_quotes:
            await to_thread(save_search, database_path, user_id, search, q)
    except Exception as e:
        LOGGER.exception("Failed to save search to database")
        # Continue processing even if saving fails, as it's not critical for user experience

    def format_quote(quote) -> str:
        route = f"{quote.origin} -> {quote.destination}"
        dates = quote.departure_date.isoformat()
        if quote.return_date:
            dates += f" - {quote.return_date.isoformat()}"

        lines = [
            f"Сервис: {quote.service}",
            f"Маршрут: {route}",
            f"Даты: {dates}",
            f"Цена: {quote.price:,} {quote.currency.upper()}".replace(",", " "),
        ]
        if getattr(quote, "airline", None):
            lines.append(f"Авиакомпания: {quote.airline}")
        if getattr(quote, "link", None):
            lines.append(f"Ссылка: {quote.link}")
        return "\n".join(lines)

    cheapest_quote = valid_quotes[0]
    other_quotes = valid_quotes[1:]

    # Send cheapest option
    cheapest_text = "🔥 Самый дешевый вариант:\n\n" + format_quote(cheapest_quote)
    await update.message.reply_text(cheapest_text, reply_markup=ReplyKeyboardRemove())

    # Send other options if available
    if other_quotes:
        others_text = "Так же в других компаниях:\n\n" + "\n\n".join(format_quote(q) for q in other_quotes)
        
        # Telegram limit is 4096 chars per message
        if len(others_text) > 4000:
            await update.message.reply_text("Так же в других компаниях:")
            for q in other_quotes:
                await update.message.reply_text(format_quote(q))
        else:
            await update.message.reply_text(others_text)

    if failed_count > 0:
        await update.message.reply_text(f"⚠️ Не удалось получить данные от {failed_count} компаний (сработала защита от ботов или нет рейсов). Загляните в консоль для деталей.")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Ввод отменен.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def normalize_iata(value: str) -> str | None:
    code = value.strip().upper()
    if len(code) == 3 and code.isalpha():
        return code
    return None


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None
