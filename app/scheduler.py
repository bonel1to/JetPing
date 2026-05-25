from __future__ import annotations

import asyncio
import logging
from asyncio import to_thread
from pathlib import Path

from telegram.ext import Application

from app.db import (
    TrackedRoute,
    list_due_tracked_routes,
    mark_tracked_route_checked,
    save_search,
    update_tracked_route_price,
)
from app.price_provider import PriceProvider, PriceProviderError, PriceQuote

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
    message = (
        "Цена снизилась.\n"
        f"Отслеживание #{route.id}\n"
        f"Маршрут: {route.origin} -> {route.destination}\n"
        f"Даты: {format_route_dates(route)}\n"
        f"Было: {old_price:,} {old_currency}\n".replace(",", " ")
        + f"Стало: {quote.price:,} {new_currency}\n".replace(",", " ")
        + f"Сервис: {quote.service}"
    )
    if quote.airline:
        message += f"\nАвиакомпания: {quote.airline}"
    if quote.link:
        message += f"\nСсылка: {quote.link}"

    await application.bot.send_message(chat_id=route.user_id, text=message)
    LOGGER.info("Price drop notification sent for route %s", route.id)
