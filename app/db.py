from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.price_provider import FlightSearch, PriceQuote

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavedSearch:
    user_id: int
    origin: str
    destination: str
    departure_date: date
    return_date: date | None
    service: str
    price: int
    currency: str
    airline: str | None
    link: str | None
    timestamp: str


@dataclass(frozen=True)
class TrackedRoute:
    id: int
    user_id: int
    origin: str
    destination: str
    departure_date: date
    return_date: date | None
    interval_minutes: int
    last_price: int | None
    currency: str | None
    is_active: bool
    created_at: str
    last_checked_at: str | None

    def to_search(self) -> FlightSearch:
        return FlightSearch(
            origin=self.origin,
            destination=self.destination,
            departure_date=self.departure_date,
            return_date=self.return_date,
        )


def init_db(db_path: Path) -> None:
    """Initializes the SQLite database, creating tables if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                departure_date TEXT NOT NULL,
                return_date TEXT,
                service TEXT NOT NULL,
                price INTEGER NOT NULL,
                currency TEXT NOT NULL,
                airline TEXT,
                link TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                departure_date TEXT NOT NULL,
                return_date TEXT,
                interval_minutes INTEGER NOT NULL,
                last_price INTEGER,
                currency TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TEXT
            )
            """
        )
        conn.commit()
    LOGGER.info("Database initialized at %s", db_path)


def save_search(db_path: Path, user_id: int, search: FlightSearch, quote: PriceQuote) -> None:
    """Saves a flight search and its result to the database."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO searches (
                user_id, origin, destination, departure_date, return_date,
                service, price, currency, airline, link
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                search.origin,
                search.destination,
                search.departure_date.isoformat(),
                search.return_date.isoformat() if search.return_date else None,
                quote.service,
                quote.price,
                quote.currency,
                quote.airline,
                quote.link,
            ),
        )
        conn.commit()
    LOGGER.info("Search saved for user %s: %s", user_id, search)


def create_tracked_route(
    db_path: Path,
    user_id: int,
    search: FlightSearch,
    interval_minutes: int,
    quote: PriceQuote | None = None,
) -> int:
    """Creates an active route tracking record and returns its id."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tracked_routes (
                user_id, origin, destination, departure_date, return_date,
                interval_minutes, last_price, currency, last_checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                user_id,
                search.origin,
                search.destination,
                search.departure_date.isoformat(),
                search.return_date.isoformat() if search.return_date else None,
                interval_minutes,
                quote.price if quote else None,
                quote.currency if quote else None,
            ),
        )
        conn.commit()
        route_id = int(cursor.lastrowid)

    LOGGER.info("Tracked route %s created for user %s: %s", route_id, user_id, search)
    return route_id


def list_tracked_routes(db_path: Path, user_id: int) -> list[TrackedRoute]:
    """Returns active route tracking records for a Telegram user."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id, user_id, origin, destination, departure_date, return_date,
                interval_minutes, last_price, currency, is_active, created_at, last_checked_at
            FROM tracked_routes
            WHERE user_id = ? AND is_active = 1
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()

    return [_row_to_tracked_route(row) for row in rows]


def list_due_tracked_routes(db_path: Path) -> list[TrackedRoute]:
    """Returns active tracking records that are due for a price check."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id, user_id, origin, destination, departure_date, return_date,
                interval_minutes, last_price, currency, is_active, created_at, last_checked_at
            FROM tracked_routes
            WHERE is_active = 1
              AND date(departure_date) >= date('now')
              AND (
                  last_checked_at IS NULL
                  OR datetime(last_checked_at, '+' || interval_minutes || ' minutes') <= datetime('now')
              )
            ORDER BY last_checked_at IS NOT NULL, last_checked_at, id
            """
        ).fetchall()

    return [_row_to_tracked_route(row) for row in rows]


def update_tracked_route_price(db_path: Path, route_id: int, quote: PriceQuote) -> None:
    """Updates the latest known price and check timestamp for a tracking record."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE tracked_routes
            SET last_price = ?, currency = ?, last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND is_active = 1
            """,
            (quote.price, quote.currency, route_id),
        )
        conn.commit()

    LOGGER.info("Tracked route %s updated with price %s %s", route_id, quote.price, quote.currency)


def mark_tracked_route_checked(db_path: Path, route_id: int) -> None:
    """Updates only last_checked_at after a failed check to avoid tight retry loops."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE tracked_routes
            SET last_checked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND is_active = 1
            """,
            (route_id,),
        )
        conn.commit()


def deactivate_tracked_route(db_path: Path, user_id: int, route_id: int) -> bool:
    """Marks a route tracking record as inactive. Returns True if a row was changed."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tracked_routes
            SET is_active = 0
            WHERE id = ? AND user_id = ? AND is_active = 1
            """,
            (route_id, user_id),
        )
        conn.commit()
        changed = cursor.rowcount > 0

    if changed:
        LOGGER.info("Tracked route %s deactivated for user %s", route_id, user_id)
    return changed


def _row_to_tracked_route(row: sqlite3.Row) -> TrackedRoute:
    return TrackedRoute(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        origin=str(row["origin"]),
        destination=str(row["destination"]),
        departure_date=date.fromisoformat(str(row["departure_date"])),
        return_date=date.fromisoformat(str(row["return_date"])) if row["return_date"] else None,
        interval_minutes=int(row["interval_minutes"]),
        last_price=int(row["last_price"]) if row["last_price"] is not None else None,
        currency=str(row["currency"]) if row["currency"] else None,
        is_active=bool(row["is_active"]),
        created_at=str(row["created_at"]),
        last_checked_at=str(row["last_checked_at"]) if row["last_checked_at"] else None,
    )
