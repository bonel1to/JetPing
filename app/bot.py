from __future__ import annotations

import logging
from html import escape
from asyncio import to_thread
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
from app.ui import build_back_keyboard, delete_tracked_ui_messages, format_airline_name, track_ui_message

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
            entry_points=[
                CommandHandler("price", price_start),
                CallbackQueryHandler(price_start_from_menu, pattern="^menu:price$"),
            ],
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
            entry_points=[
                CommandHandler("track", track_start),
                CallbackQueryHandler(track_start_from_menu, pattern="^menu:track$"),
            ],
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
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:(list|delete|help|back)$"))
    return application


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_main_menu(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_help_message(update, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    action = query.data
    if action == "menu:list":
        await list_tracks(update, context)
    elif action == "menu:delete":
        await send_ui_text(update, context, "Чтобы удалить отслеживание, отправьте команду с ID: /delete 1", reply_markup=build_back_keyboard())
    elif action == "menu:help":
        await send_help_message(update, context)
    elif action == "menu:back":
        await send_main_menu(update, context)


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else (user.first_name if user else "пользователь")
    username = escape(username)

    caption = (
        f"Привет, <b>{username}</b>.\n\n"
        "Я помогу быстро проверить цену на авиабилет, сохранить маршрут "
        "и уведомить тебя, если цена станет ниже.\n\n"
        "<b>Формат ввода</b>\n"
        "Маршрут: <code>MOW</code> -> <code>LED</code>\n"
        "Дата: <code>YYYY-MM-DD</code>\n"
        "Возврат: дата или <code>-</code> для билета в одну сторону\n\n"
        "Выберите действие ниже."
    )

    await send_ui_text(update, context, caption, reply_markup=build_main_menu_keyboard(), parse_mode="HTML")


async def send_help_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_ui_text(
        update,
        context,
        "Команды:\n"
        "/price - проверить текущую цену\n"
        "/track - сохранить маршрут для будущего отслеживания\n"
        "/list - показать активные отслеживания\n"
        "/delete <id> - удалить отслеживание\n"
        "/cancel - отменить ввод\n\n"
        "Формат даты: YYYY-MM-DD.\n"
        "Города пока вводятся IATA-кодами: MOW, LED, AER, KZN.",
        reply_markup=build_back_keyboard(),
    )


async def send_ui_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs: object) -> None:
    chat = update.effective_chat
    if chat is None:
        return

    await delete_current_user_message(update)
    extra_message_ids = []
    if update.callback_query and update.callback_query.message:
        extra_message_ids.append(update.callback_query.message.message_id)
    await delete_tracked_ui_messages(context.bot, context.bot_data, chat.id, extra_message_ids)

    message = await context.bot.send_message(chat_id=chat.id, text=text, **kwargs)
    track_ui_message(context.bot_data, chat.id, message.message_id)


async def delete_current_user_message(update: Update) -> None:
    if update.message is None:
        return

    try:
        await update.message.delete()
    except Exception:
        LOGGER.debug("Failed to delete user message", exc_info=True)


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Проверить стоимость", callback_data="menu:price"),
                InlineKeyboardButton("Отслеживать маршрут", callback_data="menu:track"),
            ],
            [InlineKeyboardButton("Активные отслеживания", callback_data="menu:list")],
            [
                InlineKeyboardButton("Удалить отслеживание", callback_data="menu:delete"),
                InlineKeyboardButton("Помощь", callback_data="menu:help"),
            ],
        ]
    )




async def price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await begin_price(update, context)


async def price_start_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    return await begin_price(update, context)


async def begin_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["flow"] = "price"
    await send_ui_text(update, context, "Введите город вылета IATA-кодом, например MOW:")
    return ORIGIN


async def origin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await send_ui_text(update, context,"Введите IATA-код из 3 букв, например MOW:")
        return ORIGIN

    context.user_data["origin"] = code
    await send_ui_text(update, context,"Введите город прилета IATA-кодом, например LED:")
    return DESTINATION


async def destination_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await send_ui_text(update, context,"Введите IATA-код из 3 букв, например LED:")
        return DESTINATION

    if code == context.user_data.get("origin"):
        await send_ui_text(update, context,"Город прилета должен отличаться от города вылета:")
        return DESTINATION

    context.user_data["destination"] = code
    await send_ui_text(update, context,"Введите дату вылета в формате YYYY-MM-DD:")
    return DEPARTURE_DATE


async def departure_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed_date = parse_iso_date(update.message.text)
    if parsed_date is None:
        await send_ui_text(update, context,"Не понял дату. Введите в формате YYYY-MM-DD:")
        return DEPARTURE_DATE

    if parsed_date < date.today():
        await send_ui_text(update, context,"Дата вылета не может быть в прошлом. Введите будущую дату:")
        return DEPARTURE_DATE

    context.user_data["departure_date"] = parsed_date
    await send_ui_text(update, context,
        "Введите дату возвращения в формате YYYY-MM-DD или отправьте '-' для билета в одну сторону:"
    )
    return RETURN_DATE


async def return_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return_date = await parse_return_date_or_reply(update, context, RETURN_DATE)
    if return_date is False:
        return RETURN_DATE

    search = build_search_from_user_data(context, return_date)
    await send_ui_text(update, context,"Проверяю цену...")

    quote = await get_quote_or_reply(update, context, search)
    if quote is None:
        return ConversationHandler.END

    await save_search_safely(update, context, search, quote)
    await send_ui_text(update, context,format_quote_message(quote), reply_markup=build_back_keyboard(), parse_mode="HTML")
    context.user_data.clear()
    return ConversationHandler.END


async def track_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await begin_track(update, context)


async def track_start_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    return await begin_track(update, context)


async def begin_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["flow"] = "track"
    await send_ui_text(update, context, "Введите город вылета для отслеживания IATA-кодом, например MOW:")
    return TRACK_ORIGIN


async def track_origin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await send_ui_text(update, context,"Введите IATA-код из 3 букв, например MOW:")
        return TRACK_ORIGIN

    context.user_data["origin"] = code
    await send_ui_text(update, context,"Введите город прилета IATA-кодом, например LED:")
    return TRACK_DESTINATION


async def track_destination_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = normalize_iata(update.message.text)
    if not code:
        await send_ui_text(update, context,"Введите IATA-код из 3 букв, например LED:")
        return TRACK_DESTINATION

    if code == context.user_data.get("origin"):
        await send_ui_text(update, context,"Город прилета должен отличаться от города вылета:")
        return TRACK_DESTINATION

    context.user_data["destination"] = code
    await send_ui_text(update, context,"Введите дату вылета в формате YYYY-MM-DD:")
    return TRACK_DEPARTURE_DATE


async def track_departure_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed_date = parse_iso_date(update.message.text)
    if parsed_date is None:
        await send_ui_text(update, context,"Не понял дату. Введите в формате YYYY-MM-DD:")
        return TRACK_DEPARTURE_DATE

    if parsed_date < date.today():
        await send_ui_text(update, context,"Дата вылета не может быть в прошлом. Введите будущую дату:")
        return TRACK_DEPARTURE_DATE

    context.user_data["departure_date"] = parsed_date
    await send_ui_text(update, context,
        "Введите дату возвращения в формате YYYY-MM-DD или отправьте '-' для билета в одну сторону:"
    )
    return TRACK_RETURN_DATE


async def track_return_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return_date = await parse_return_date_or_reply(update, context, TRACK_RETURN_DATE)
    if return_date is False:
        return TRACK_RETURN_DATE

    context.user_data["return_date"] = return_date
    await send_ui_text(update, context,
        "Введите интервал проверки в минутах, например 1, 5, 10, 30 или 60.\n"
        "Также можно написать: 1 час, 10 часов, 24 часа."
    )
    return TRACK_INTERVAL


async def track_interval_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    interval = parse_interval_minutes(update.message.text)
    if interval is None:
        await send_ui_text(update, context,"Не понял интервал. Введите любое положительное число минут, например 5:")
        return TRACK_INTERVAL

    search = build_search_from_user_data(context, context.user_data.get("return_date"))
    await send_ui_text(update, context,"Проверяю текущую цену и сохраняю отслеживание...")

    quote = await get_quote_or_reply(update, context, search)
    if quote is None:
        return ConversationHandler.END

    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    try:
        route_id = await to_thread(create_tracked_route, database_path, user_id, search, interval, quote)
    except Exception:
        LOGGER.exception("Failed to create tracked route")
        await send_ui_text(update, context,"Не удалось сохранить отслеживание. Попробуйте позже.", reply_markup=build_back_keyboard())
        return ConversationHandler.END

    await save_search_safely(update, context, search, quote)

    await send_ui_text(update, context,
        "<b>Отслеживание сохранено</b>\n"
        f"ID: <code>{route_id}</code>\n"
        f"Маршрут: <b>{search.origin} -> {search.destination}</b>\n"
        f"Интервал: {format_interval(interval)}\n"
        f"Текущая стоимость: <b>{quote.price:,} {quote.currency.upper()}</b>".replace(",", " "),
        reply_markup=build_back_keyboard(),
        parse_mode="HTML",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def list_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    routes = await to_thread(list_tracked_routes, database_path, user_id)

    if not routes:
        await send_ui_text(update, context, "Активных отслеживаний пока нет. Создайте первое через /track.", reply_markup=build_back_keyboard())
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

    await send_ui_text(update, context, "\n\n".join(blocks), reply_markup=build_back_keyboard())


async def delete_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_ui_text(update, context,"Укажите ID отслеживания: /delete 1", reply_markup=build_back_keyboard())
        return

    try:
        route_id = int(context.args[0])
    except ValueError:
        await send_ui_text(update, context,"ID должен быть числом. Пример: /delete 1", reply_markup=build_back_keyboard())
        return

    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    deleted = await to_thread(deactivate_tracked_route, database_path, user_id, route_id)

    if deleted:
        await send_ui_text(update, context,f"Отслеживание #{route_id} удалено.", reply_markup=build_back_keyboard())
    else:
        await send_ui_text(update, context,f"Активное отслеживание #{route_id} не найдено.", reply_markup=build_back_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_ui_text(update, context,"Ввод отменен.", reply_markup=build_back_keyboard())
    return ConversationHandler.END


async def parse_return_date_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int) -> date | None | bool:
    text = update.message.text.strip()
    if text == "-":
        return None

    return_date = parse_iso_date(text)
    if return_date is None:
        await send_ui_text(update, context,"Не понял дату. Введите YYYY-MM-DD или '-' для билета в одну сторону:")
        return False

    if return_date < context.user_data["departure_date"]:
        await send_ui_text(update, context,"Дата возвращения не может быть раньше даты вылета:")
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
        await send_ui_text(update, context,f"Не удалось получить цену: {exc}", reply_markup=build_back_keyboard())
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
    route = escape(f"{quote.origin} -> {quote.destination}")
    dates = quote.departure_date.isoformat()
    if quote.return_date:
        dates += f" - {quote.return_date.isoformat()}"

    lines = [
        "<b>Найдена стоимость</b>",
        f"Маршрут: <b>{route}</b>",
        f"Даты: <code>{escape(dates)}</code>",
        f"Стоимость: <b>{quote.price:,} {quote.currency.upper()}</b>".replace(",", " "),
    ]
    airline_name = format_airline_name(quote.airline)
    if airline_name:
        lines.append(f"Авиакомпания: {escape(airline_name)}")
    if quote.link:
        lines.append(f"Ссылка: {escape(quote.link)}")
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






















