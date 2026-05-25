from __future__ import annotations

import asyncio
import logging
from html import escape
from asyncio import to_thread
from pathlib import Path

from telegram.ext import Application

from app.input_parser import format_route
from app.db import (
    TrackedRoute,
    list_due_tracked_routes,
    mark_tracked_route_checked,
    save_search,
    update_tracked_route_price,
)
from app.price_provider import PriceProvider, PriceProviderError, PriceQuote
from app.ui import build_back_keyboard, delete_tracked_ui_messages, format_airline_name, track_ui_message

LOGGER = logging.getLogger(__name__)

SCHEDULER_TASK_KEY = "scheduler_task"


async def start_scheduler(application: Application) -> None:
    tick_seconds = int(application.bot_data.get("scheduler_tick_seconds", 60))
    task = asyncio.create_task(_scheduler_loop(application, tick_seconds), name="jetping-price-scheduler")
    application.bot_data[SCHEDULER_TASK_KEY] = task
    LOGGER.info("Price scheduler started with %s second tick", tick_seconds)


async def stop_scheduler(application: Application) -> None:
    task = application.bot_data.get(SCHEDULER_TASK_KEY)
    if not task:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    LOGGER.info("Price scheduler stopped")


async def _scheduler_loop(application: Application, tick_seconds: int) -> None:
    while True:
        try:
            await check_due_routes(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Unexpected scheduler iteration error")

        await asyncio.sleep(tick_seconds)


async def check_due_routes(application: Application) -> None:
    database_path: Path = application.bot_data["database_path"]
    provider: PriceProvider = application.bot_data["price_provider"]

    routes = await to_thread(list_due_tracked_routes, database_path)
    if not routes:
        return

    LOGGER.info("Scheduler found %s due route(s)", len(routes))
    for route in routes:
        await check_one_route(application, database_path, provider, route)


async def check_one_route(
    application: Application,
    database_path: Path,
    provider: PriceProvider,
    route: TrackedRoute,
) -> None:
    search = route.to_search()

    try:
        quote = await to_thread(provider.get_lowest_price, search)
    except PriceProviderError as exc:
        LOGGER.warning("Scheduled check failed for route %s: %s", route.id, exc)
        await to_thread(mark_tracked_route_checked, database_path, route.id)
        return
    except Exception:
        LOGGER.exception("Unexpected scheduled check error for route %s", route.id)
        await to_thread(mark_tracked_route_checked, database_path, route.id)
        return

    await to_thread(save_search, database_path, route.user_id, search, quote)
    old_price = route.last_price
    await to_thread(update_tracked_route_price, database_path, route.id, quote)

    if old_price is not None and quote.price < old_price:
        await notify_price_drop(application, route, quote, old_price)


def format_route_dates(route: TrackedRoute) -> str:
    dates = route.departure_date.isoformat()
    if route.return_date:
        dates += f" - {route.return_date.isoformat()}"
    return dates


async def notify_price_drop(
    application: Application,
    route: TrackedRoute,
    quote: PriceQuote,
    old_price: int,
) -> None:
    old_currency = (route.currency or quote.currency).upper()
    new_currency = quote.currency.upper()
    route_text = escape(format_route(route.origin, route.destination))
    dates = escape(format_route_dates(route))
    message = (
        "<b>Цена снизилась</b>\n"
        f"Отслеживание: <code>#{route.id}</code>\n"
        f"Маршрут: <b>{route_text}</b>\n"
        f"Даты: <code>{dates}</code>\n"
        f"Было: {old_price:,} {old_currency}\n".replace(",", " ")
        + f"Стало: <b>{quote.price:,} {new_currency}</b>".replace(",", " ")
    )
    airline_name = format_airline_name(quote.airline)
    if airline_name:
        message += f"\nАвиакомпания: {escape(airline_name)}"
    if quote.link:
        message += f"\nСсылка: {escape(quote.link)}"

    await delete_tracked_ui_messages(application.bot, application.bot_data, route.user_id)
    sent_message = await application.bot.send_message(
        chat_id=route.user_id,
        text=message,
        reply_markup=build_back_keyboard(),
        parse_mode="HTML",
    )
    track_ui_message(application.bot_data, route.user_id, sent_message.message_id)
    LOGGER.info("Price drop notification sent for route %s", route.id)




