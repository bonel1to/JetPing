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
    timestamp: str # ISO format for datetime when saved


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