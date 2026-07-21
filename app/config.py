from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_ids: frozenset[int]
    database_path: Path
    travelpayouts_token: str | None = None
    currency: str = "rub"
    market: str = "ru"
    scheduler_tick_seconds: int = 60


def load_settings(require_telegram_token: bool = True) -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if require_telegram_token and not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to .env.")

    admin_ids_raw = os.getenv("JETPING_ADMIN_IDS", "")
    admin_ids = frozenset(
        int(value.strip()) for value in admin_ids_raw.split(",") if value.strip().isdigit()
    )

    return Settings(
        telegram_bot_token=token,
        admin_ids=admin_ids,
        database_path=Path(os.getenv("JETPING_DATABASE_PATH", "data/jetping.db")),
        travelpayouts_token=os.getenv("TRAVELPAYOUTS_TOKEN", "").strip() or None,
        currency=os.getenv("JETPING_CURRENCY", "rub").strip().lower(),
        market=os.getenv("JETPING_MARKET", "ru").strip().lower(),
        scheduler_tick_seconds=parse_positive_int(os.getenv("JETPING_SCHEDULER_TICK_SECONDS"), default=60),
    )


def parse_positive_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default

    try:
        parsed = int(value)
    except ValueError:
        return default

    return parsed if parsed > 0 else default
