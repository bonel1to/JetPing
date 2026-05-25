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
from app.db import create_tracked_route, deactivate_tracked_route, list_tracked_routes, save_search
from app.price_provider import FlightSearch, PriceProvider, PriceProviderError, PriceQuote, build_price_provider
from app.scheduler import start_scheduler, stop_scheduler

LOGGER = logging.getLogger(__name__)

ORIGIN, DESTINATION, DEPARTURE_DATE, RETURN_DATE = range(4)
TRACK_ORIGIN, TRACK_DESTINATION, TRACK_DEPARTURE_DATE, TRACK_RETURN_DATE, TRACK_INTERVAL = range(10, 15)

INTERVAL_OPTIONS = {
    "1 минута": 1,
    "минута": 1,
    "5 минут": 5,
    "10 минут": 10,
    "30 минут": 30,
    "1 час": 60,
    "час": 60,
    "10 часов": 600,
    "24 часа": 1440,
    "сутки": 1440,
}


def create_application(settings: Settings) -> Application:
    provider = build_price_provider(
        name=settings.price_provider,
        token=settings.travelpayouts_token,
        currency=settings.currency,
        market=settings.market,
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(start_scheduler)
        .post_shutdown(stop_scheduler)
        .build()
    )
    application.bot_data["price_provider"] = provider
    application.bot_data["database_path"] = settings.database_path
    application.bot_data["admin_ids"] = settings.admin_ids
    application.bot_data["scheduler_tick_seconds"] = settings.scheduler_tick_seconds

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_tracks))
    application.add_handler(CommandHandler("delete", delete_track))
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
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("track", track_start)],
            states={
                TRACK_ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_origin_received)],
                TRACK_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_destination_received)],
                TRACK_DEPARTURE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_departure_date_received)],
                TRACK_RETURN_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_return_date_received)],
                TRACK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_interval_received)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    admin_ids = context.bot_data.get("admin_ids", set())
    greeting = "Привет, суперпользователь!\n\n" if user_id in admin_ids else ""

    await update.message.reply_text(
        f"{greeting}JetPing отслеживает цены на авиабилеты.\n\n"
        "Доступно сейчас:\n"
        "/price - разовая проверка цены\n"
        "/track - сохранить маршрут для отслеживания\n"
        "/list - показать активные отслеживания\n"
        "/delete <id> - удалить отслеживание"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/price - проверить текущую цену\n"
        "/track - сохранить маршрут для будущего отслеживания\n"
        "/list - показать активные отслеживания\n"
        "/delete <id> - удалить отслеживание\n"
        "/cancel - отменить ввод\n\n"
        "Формат даты: YYYY-MM-DD.\n"
        "Города пока вводятся IATA-кодами: MOW, LED, AER, KZN."
    )


async def price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["flow"] = "price"
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
    return_date = await parse_return_date_or_reply(update, context, RETURN_DATE)
    if return_date is False:
        return RETURN_DATE

    search = build_search_from_user_data(context, return_date)
    await update.message.reply_text("Проверяю цену...")

    quote = await get_quote_or_reply(update, context, search)
    if quote is None:
        return ConversationHandler.END

    await save_search_safely(update, context, search, quote)
    await update.message.reply_text(format_quote_message(quote), reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


async def track_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["flow"] = "track"
    await update.message.reply_text("Введите город вылета для отслеживания IATA-кодом, например MOW:")
    return TRACK_ORIGIN


async def track_origin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await update.message.reply_text("Введите IATA-код из 3 букв, например MOW:")
        return TRACK_ORIGIN

    context.user_data["origin"] = code
    await update.message.reply_text("Введите город прилета IATA-кодом, например LED:")
    return TRACK_DESTINATION


async def track_destination_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await update.message.reply_text("Введите IATA-код из 3 букв, например LED:")
        return TRACK_DESTINATION

    if code == context.user_data.get("origin"):
        await update.message.reply_text("Город прилета должен отличаться от города вылета:")
        return TRACK_DESTINATION

    context.user_data["destination"] = code
    await update.message.reply_text("Введите дату вылета в формате YYYY-MM-DD:")
    return TRACK_DEPARTURE_DATE


async def track_departure_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed_date = parse_iso_date(update.message.text)
    if parsed_date is None:
        await update.message.reply_text("Не понял дату. Введите в формате YYYY-MM-DD:")
        return TRACK_DEPARTURE_DATE

    if parsed_date < date.today():
        await update.message.reply_text("Дата вылета не может быть в прошлом. Введите будущую дату:")
        return TRACK_DEPARTURE_DATE

    context.user_data["departure_date"] = parsed_date
    await update.message.reply_text(
        "Введите дату возвращения в формате YYYY-MM-DD или отправьте '-' для билета в одну сторону:"
    )
    return TRACK_RETURN_DATE


async def track_return_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return_date = await parse_return_date_or_reply(update, context, TRACK_RETURN_DATE)
    if return_date is False:
        return TRACK_RETURN_DATE

    context.user_data["return_date"] = return_date
    await update.message.reply_text(
        "Введите интервал проверки в минутах, например 1, 5, 10, 30 или 60.\n"
        "Также можно написать: 1 час, 10 часов, 24 часа."
    )
    return TRACK_INTERVAL


async def track_interval_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    interval = parse_interval_minutes(update.message.text)
    if interval is None:
        await update.message.reply_text("Не понял интервал. Введите любое положительное число минут, например 5:")
        return TRACK_INTERVAL

    search = build_search_from_user_data(context, context.user_data.get("return_date"))
    await update.message.reply_text("Проверяю текущую цену и сохраняю отслеживание...")

    quote = await get_quote_or_reply(update, context, search)
    if quote is None:
        return ConversationHandler.END

    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    try:
        route_id = await to_thread(create_tracked_route, database_path, user_id, search, interval, quote)
    except Exception:
        LOGGER.exception("Failed to create tracked route")
        await update.message.reply_text("Не удалось сохранить отслеживание. Попробуйте позже.")
        return ConversationHandler.END

    await save_search_safely(update, context, search, quote)

    await update.message.reply_text(
        "Отслеживание сохранено.\n"
        f"ID: {route_id}\n"
        f"Маршрут: {search.origin} -> {search.destination}\n"
        f"Интервал: {format_interval(interval)}\n"
        f"Текущая цена: {quote.price:,} {quote.currency.upper()}".replace(",", " ")
    )
    context.user_data.clear()
    return ConversationHandler.END


async def list_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    routes = await to_thread(list_tracked_routes, database_path, user_id)

    if not routes:
        await update.message.reply_text("Активных отслеживаний пока нет. Создайте первое через /track.")
        return

    blocks = ["Активные отслеживания:"]
    for route in routes:
        dates = route.departure_date.isoformat()
        if route.return_date:
            dates += f" - {route.return_date.isoformat()}"
        price = "неизвестно"
        if route.last_price is not None and route.currency:
            price = f"{route.last_price:,} {route.currency.upper()}".replace(",", " ")
        blocks.append(
            f"#{route.id}: {route.origin} -> {route.destination}\n"
            f"Даты: {dates}\n"
            f"Интервал: {format_interval(route.interval_minutes)}\n"
            f"Последняя цена: {price}"
        )

    await update.message.reply_text("\n\n".join(blocks))


async def delete_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Укажите ID отслеживания: /delete 1")
        return

    try:
        route_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Пример: /delete 1")
        return

    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    deleted = await to_thread(deactivate_tracked_route, database_path, user_id, route_id)

    if deleted:
        await update.message.reply_text(f"Отслеживание #{route_id} удалено.")
    else:
        await update.message.reply_text(f"Активное отслеживание #{route_id} не найдено.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Ввод отменен.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def parse_return_date_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int) -> date | None | bool:
    text = update.message.text.strip()
    if text == "-":
        return None

    return_date = parse_iso_date(text)
    if return_date is None:
        await update.message.reply_text("Не понял дату. Введите YYYY-MM-DD или '-' для билета в одну сторону:")
        return False

    if return_date < context.user_data["departure_date"]:
        await update.message.reply_text("Дата возвращения не может быть раньше даты вылета:")
        return False

    return return_date


def build_search_from_user_data(context: ContextTypes.DEFAULT_TYPE, return_date: date | None) -> FlightSearch:
    return FlightSearch(
        origin=context.user_data["origin"],
        destination=context.user_data["destination"],
        departure_date=context.user_data["departure_date"],
        return_date=return_date,
    )


async def get_quote_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, search: FlightSearch) -> PriceQuote | None:
    provider: PriceProvider = context.bot_data["price_provider"]
    try:
        return await to_thread(provider.get_lowest_price, search)
    except PriceProviderError as exc:
        LOGGER.exception("Price lookup failed")
        await update.message.reply_text(f"Не удалось получить цену: {exc}")
        return None


async def save_search_safely(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    search: FlightSearch,
    quote: PriceQuote,
) -> None:
    try:
        user_id = update.effective_user.id
        database_path = context.bot_data["database_path"]
        await to_thread(save_search, database_path, user_id, search, quote)
    except Exception:
        LOGGER.exception("Failed to save search to database")


def format_quote_message(quote: PriceQuote) -> str:
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
    return "\n".join(lines)


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


def parse_interval_minutes(value: str) -> int | None:
    normalized = value.strip().lower()
    if normalized in INTERVAL_OPTIONS:
        return INTERVAL_OPTIONS[normalized]

    try:
        minutes = int(normalized)
    except ValueError:
        return None

    if minutes < 1:
        return None
    return minutes


def format_interval(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "24 часа" if days == 1 else f"{days} дн."
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours == 1:
            return "1 час"
        if 2 <= hours <= 4:
            return f"{hours} часа"
        return f"{hours} часов"
    return f"{minutes} минут"



