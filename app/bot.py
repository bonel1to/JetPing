from __future__ import annotations

import logging
from asyncio import to_thread
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
from app.price_provider import FlightSearch, PriceProvider, PriceProviderError, build_price_provider

LOGGER = logging.getLogger(__name__)

ORIGIN, DESTINATION, DEPARTURE_DATE, RETURN_DATE = range(4)


def create_application(settings: Settings) -> Application:
    provider = build_price_provider(
        name=settings.price_provider,
        token=settings.travelpayouts_token,
        currency=settings.currency,
        market=settings.market,
    )

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["price_provider"] = provider

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
    await update.message.reply_text(
        "JetPing отслеживает цены на авиабилеты.\n\n"
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

    await update.message.reply_text("Проверяю цену...")

    provider: PriceProvider = context.bot_data["price_provider"]
    try:
        quote = await to_thread(provider.get_lowest_price, search)
    except PriceProviderError as exc:
        LOGGER.exception("Price lookup failed")
        await update.message.reply_text(f"Не удалось получить цену: {exc}")
        return ConversationHandler.END

    route = f"{quote.origin} -> {quote.destination}"
    dates = quote.departure_date.isoformat()
    if quote.return_date:
        dates += f" - {quote.return_date.isoformat()}"

    lines = [
        "Найдена цена:",
        f"Сервис: {quote.service}",
        f"Маршрут: {route}",
        f"Даты: {dates}",
        f"Цена: {quote.price:,} {quote.currency.upper()}".replace(",", " "),
    ]
    if quote.airline:
        lines.append(f"Авиакомпания: {quote.airline}")
    if quote.link:
        lines.append(f"Ссылка: {quote.link}")

    await update.message.reply_text("\n".join(lines), reply_markup=ReplyKeyboardRemove())
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
