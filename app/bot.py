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
from app.input_parser import format_city, format_route, parse_city_code, parse_user_date, resolve_city_code
from app.price_provider import FlightSearch, PriceProvider, PriceProviderError, PriceQuote, build_price_provider
from app.scheduler import start_scheduler, stop_scheduler
from app.ui import build_back_keyboard, delete_tracked_ui_messages, format_airline_name, track_ui_message

LOGGER = logging.getLogger(__name__)

ORIGIN, ORIGIN_CONFIRM, DESTINATION, DESTINATION_CONFIRM, DEPARTURE_DATE, RETURN_DATE = range(6)
TRACK_ORIGIN, TRACK_ORIGIN_CONFIRM, TRACK_DESTINATION, TRACK_DESTINATION_CONFIRM, TRACK_DEPARTURE_DATE, TRACK_RETURN_DATE, TRACK_INTERVAL = range(10, 17)


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
                ORIGIN_CONFIRM: [CallbackQueryHandler(price_origin_confirmation, pattern="^city:price_origin:(yes|no)$")],
                DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, destination_received)],
                DESTINATION_CONFIRM: [CallbackQueryHandler(price_destination_confirmation, pattern="^city:price_destination:(yes|no)$")],
                DEPARTURE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, departure_date_received)],
                RETURN_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, return_date_received),
                    CallbackQueryHandler(return_date_skip, pattern="^date:price_return:none$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", cancel),
                CallbackQueryHandler(cancel_to_menu, pattern="^menu:back$"),
            ],
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
                TRACK_ORIGIN_CONFIRM: [CallbackQueryHandler(track_origin_confirmation, pattern="^city:track_origin:(yes|no)$")],
                TRACK_DESTINATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_destination_received)],
                TRACK_DESTINATION_CONFIRM: [CallbackQueryHandler(track_destination_confirmation, pattern="^city:track_destination:(yes|no)$")],
                TRACK_DEPARTURE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_departure_date_received)],
                TRACK_RETURN_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, track_return_date_received),
                    CallbackQueryHandler(track_return_date_skip, pattern="^date:track_return:none$"),
                ],
                TRACK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_interval_received)],
            },
            fallbacks=[
                CommandHandler("cancel", cancel),
                CallbackQueryHandler(cancel_to_menu, pattern="^menu:back$"),
            ],
        )
    )
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:(list|delete|back)$"))
    application.add_handler(CallbackQueryHandler(delete_track_callback, pattern="^delete:[0-9]+$"))
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
        await show_delete_tracks_menu(update, context)
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
        "Маршрут: <code>Москва</code> -> <code>Санкт-Петербург</code>\n"
        "Дата: <code>8 июня 2026</code> или <code>2026-06-08</code>\n"
        "Возврат: дата или кнопка <b>Без обратного билета</b>\n\n"
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
        "Формат даты: 8 июня 2026 или YYYY-MM-DD.\n"
        "Города можно вводить названиями: Москва, Санкт-Петербург, Сочи, Казань.",
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
            [InlineKeyboardButton("Проверить стоимость", callback_data="menu:price")],
            [InlineKeyboardButton("Отслеживать маршрут", callback_data="menu:track")],
            [InlineKeyboardButton("Активные отслеживания", callback_data="menu:list")],
            [InlineKeyboardButton("Удалить отслеживание", callback_data="menu:delete")],
        ]
    )



def build_city_confirmation_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да", callback_data=f"city:{callback_prefix}:yes"),
                InlineKeyboardButton("Нет", callback_data=f"city:{callback_prefix}:no"),
            ]
        ]
    )



def build_no_return_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Без обратного билета", callback_data=callback_data)]])
async def price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await begin_price(update, context)

async def price_start_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    return await begin_price(update, context)


async def begin_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["flow"] = "price"
    await send_ui_text(update, context, "Введите город вылета, например Москва или MOW:")
    return ORIGIN


async def origin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = await resolve_city_or_reply(
        update,
        context,
        retry_message="Не нашел город. Введите название, например Москва или Нью-Йорк, либо IATA-код MOW:",
    )
    if code is None:
        return ORIGIN

    await ask_city_confirmation(update, context, code, "price_origin")
    return ORIGIN_CONFIRM


async def price_origin_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_city_confirmation(
        update,
        context,
        field="origin",
        retry_state=ORIGIN,
        next_state=DESTINATION,
        retry_message="Введите город вылета, например Москва или Нью-Йорк:",
        next_message="Введите город прилета, например Санкт-Петербург, Нью-Йорк или LED:",
    )


async def destination_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = await resolve_city_or_reply(
        update,
        context,
        retry_message="Не нашел город. Введите название, например Санкт-Петербург или Нью-Йорк, либо IATA-код LED:",
    )
    if code is None:
        return DESTINATION

    if code == context.user_data.get("origin"):
        await send_ui_text(update, context, "Город прилета должен отличаться от города вылета:")
        return DESTINATION

    await ask_city_confirmation(update, context, code, "price_destination")
    return DESTINATION_CONFIRM


async def price_destination_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_city_confirmation(
        update,
        context,
        field="destination",
        retry_state=DESTINATION,
        next_state=DEPARTURE_DATE,
        retry_message="Введите город прилета, например Санкт-Петербург или Нью-Йорк:",
        next_message="Введите дату вылета, например 8 июня 2026 или 2026-06-08:",
    )


async def departure_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed_date = parse_iso_date(update.message.text)
    if parsed_date is None:
        await send_ui_text(update, context,"Не понял дату. Введите, например 8 июня 2026 или 2026-06-08:")
        return DEPARTURE_DATE

    if parsed_date < date.today():
        await send_ui_text(update, context,"Дата вылета не может быть в прошлом. Введите будущую дату:")
        return DEPARTURE_DATE

    context.user_data["departure_date"] = parsed_date
    await ask_return_date(update, context, "date:price_return:none")
    return RETURN_DATE



async def return_date_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    return await finish_price_search(update, context, None)
async def return_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return_date = await parse_return_date_or_reply(update, context, RETURN_DATE)
    if return_date is False:
        return RETURN_DATE

    return await finish_price_search(update, context, return_date)


async def finish_price_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    return_date: date | None,
) -> int:
    search = build_search_from_user_data(context, return_date)
    await send_ui_text(update, context, "Проверяю цену...")

    quote = await get_quote_or_reply(update, context, search)
    if quote is None:
        return ConversationHandler.END

    await save_search_safely(update, context, search, quote)
    await send_ui_text(update, context, format_quote_message(quote), reply_markup=build_back_keyboard(), parse_mode="HTML")
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
    await send_ui_text(update, context, "Введите город вылета для отслеживания, например Москва или MOW:")
    return TRACK_ORIGIN


async def track_origin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = await resolve_city_or_reply(
        update,
        context,
        retry_message="Не нашел город. Введите название, например Москва или Нью-Йорк, либо IATA-код MOW:",
    )
    if code is None:
        return TRACK_ORIGIN

    await ask_city_confirmation(update, context, code, "track_origin")
    return TRACK_ORIGIN_CONFIRM


async def track_origin_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_city_confirmation(
        update,
        context,
        field="origin",
        retry_state=TRACK_ORIGIN,
        next_state=TRACK_DESTINATION,
        retry_message="Введите город вылета для отслеживания, например Москва или Нью-Йорк:",
        next_message="Введите город прилета, например Санкт-Петербург, Нью-Йорк или LED:",
    )


async def track_destination_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = await resolve_city_or_reply(
        update,
        context,
        retry_message="Не нашел город. Введите название, например Санкт-Петербург или Нью-Йорк, либо IATA-код LED:",
    )
    if code is None:
        return TRACK_DESTINATION

    if code == context.user_data.get("origin"):
        await send_ui_text(update, context, "Город прилета должен отличаться от города вылета:")
        return TRACK_DESTINATION

    await ask_city_confirmation(update, context, code, "track_destination")
    return TRACK_DESTINATION_CONFIRM


async def track_destination_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_city_confirmation(
        update,
        context,
        field="destination",
        retry_state=TRACK_DESTINATION,
        next_state=TRACK_DEPARTURE_DATE,
        retry_message="Введите город прилета, например Санкт-Петербург или Нью-Йорк:",
        next_message="Введите дату вылета, например 8 июня 2026 или 2026-06-08:",
    )


async def track_departure_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed_date = parse_iso_date(update.message.text)
    if parsed_date is None:
        await send_ui_text(update, context,"Не понял дату. Введите, например 8 июня 2026 или 2026-06-08:")
        return TRACK_DEPARTURE_DATE

    if parsed_date < date.today():
        await send_ui_text(update, context,"Дата вылета не может быть в прошлом. Введите будущую дату:")
        return TRACK_DEPARTURE_DATE

    context.user_data["departure_date"] = parsed_date
    await ask_return_date(update, context, "date:track_return:none")
    return TRACK_RETURN_DATE



async def track_return_date_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["return_date"] = None
    await ask_track_interval(update, context)
    return TRACK_INTERVAL
async def track_return_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return_date = await parse_return_date_or_reply(update, context, TRACK_RETURN_DATE)
    if return_date is False:
        return TRACK_RETURN_DATE

    context.user_data["return_date"] = return_date
    await ask_track_interval(update, context)
    return TRACK_INTERVAL


async def ask_track_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_ui_text(
        update,
        context,
        "Введите интервал проверки в минутах, например 1, 5, 10, 30 или 60.\n"
        "Также можно написать: 1 час, 10 часов, 24 часа.",
    )


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
        f"Маршрут: <b>{escape(format_route(search.origin, search.destination))}</b>\n"
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
            f"#{route.id}: {format_route(route.origin, route.destination)}\n"
            f"Даты: {dates}\n"
            f"Интервал: {format_interval(route.interval_minutes)}\n"
            f"Последняя цена: {price}"
        )

    await send_ui_text(update, context, "\n\n".join(blocks), reply_markup=build_back_keyboard())



async def show_delete_tracks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    routes = await to_thread(list_tracked_routes, database_path, user_id)

    if not routes:
        await send_ui_text(update, context, "Активных отслеживаний пока нет.", reply_markup=build_back_keyboard())
        return

    await send_ui_text(
        update,
        context,
        "Выберите отслеживание, которое нужно удалить:",
        reply_markup=build_delete_tracks_keyboard(routes),
    )


def build_delete_tracks_keyboard(routes: list[object]) -> InlineKeyboardMarkup:
    buttons = []
    for route in routes:
        label = f"#{route.id}: {format_route(route.origin, route.destination)}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"delete:{route.id}")])
    buttons.append([InlineKeyboardButton("Вернуться в меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(buttons)


async def delete_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    await query.answer()
    route_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id
    database_path = context.bot_data["database_path"]
    deleted = await to_thread(deactivate_tracked_route, database_path, user_id, route_id)

    if deleted:
        await send_ui_text(update, context, f"Отслеживание #{route_id} удалено.", reply_markup=build_back_keyboard())
    else:
        await send_ui_text(update, context, f"Активное отслеживание #{route_id} не найдено.", reply_markup=build_back_keyboard())


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


async def cancel_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.clear()
    await send_main_menu(update, context)
    return ConversationHandler.END


async def ask_return_date(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data: str) -> None:
    await send_ui_text(
        update,
        context,
        "Введите дату возвращения, например 15 июня 2026:",
        reply_markup=build_no_return_keyboard(callback_data),
    )
async def parse_return_date_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int) -> date | None | bool:
    text = update.message.text.strip()
    return_date = parse_iso_date(text)
    if return_date is None:
        await send_ui_text(update, context, "Не понял дату. Введите, например 15 июня 2026, или нажмите кнопку Без обратного билета.")
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
    route = escape(format_route(quote.origin, quote.destination))
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
        lines.append(f'<a href="{escape(quote.link, quote=True)}">Ссылка</a>')
    return "\n".join(lines)


async def ask_city_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    code: str,
    callback_prefix: str,
) -> None:
    context.user_data["pending_city_code"] = code
    await send_ui_text(
        update,
        context,
        f"Город: <b>{escape(format_city(code))}</b>\nВерно?",
        reply_markup=build_city_confirmation_keyboard(callback_prefix),
        parse_mode="HTML",
    )


async def handle_city_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    field: str,
    retry_state: int,
    next_state: int,
    retry_message: str,
    next_message: str,
) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    answer = query.data.rsplit(":", 1)[-1] if query and query.data else "no"
    if answer == "no":
        context.user_data.pop("pending_city_code", None)
        await send_ui_text(update, context, retry_message)
        return retry_state

    code = context.user_data.pop("pending_city_code", None)
    if not code:
        await send_ui_text(update, context, retry_message)
        return retry_state

    context.user_data[field] = code
    await send_ui_text(update, context, next_message)
    return next_state
async def resolve_city_or_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    retry_message: str,
) -> str | None:
    code = await to_thread(resolve_city_code, update.message.text)
    if code is None:
        await send_ui_text(update, context, retry_message)
        return None
    return code

def normalize_iata(value: str) -> str | None:
    return parse_city_code(value)


def parse_iso_date(value: str) -> date | None:
    return parse_user_date(value)


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

















































