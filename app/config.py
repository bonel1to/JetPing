from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    # Сначала все поля БЕЗ значений по умолчанию (обязательные)
    telegram_bot_token: str
    admin_ids: frozenset[int]
    database_path: Path
    
    # Затем поля СО значениями по умолчанию (опциональные)
    price_providers: tuple[str, ...] = ("mock",)
    travelpayouts_token: str | None = None
    currency: str = "rub"
    market: str = "ru"


def load_settings(require_telegram_token: bool = True) -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if require_telegram_token and not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to .env.")

    admin_ids_raw = os.getenv("JETPING_ADMIN_IDS", "")
    admin_ids = frozenset(
        int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()
    )

    providers_raw = os.getenv("JETPING_PRICE_PROVIDERS", os.getenv("JETPING_PRICE_PROVIDER", "mock"))
    price_providers = tuple(p.strip().lower() for p in providers_raw.split(",") if p.strip())
    if not price_providers:
        price_providers = ("mock",)

    return Settings(
        telegram_bot_token=token,
        admin_ids=admin_ids,
        database_path=Path(os.getenv("JETPING_DATABASE_PATH", "data/jetping.db")),
        price_providers=price_providers,
        travelpayouts_token=os.getenv("TRAVELPAYOUTS_TOKEN", "").strip() or None,
        currency=os.getenv("JETPING_CURRENCY", "rub").strip().lower(),
        market=os.getenv("JETPING_MARKET", "ru").strip().lower(),
    )