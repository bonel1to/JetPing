from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    price_provider: str = "mock"
    travelpayouts_token: str | None = None
    currency: str = "rub"
    market: str = "ru"


def load_settings(require_telegram_token: bool = True) -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if require_telegram_token and not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to .env.")

    return Settings(
        telegram_bot_token=token,
        price_provider=os.getenv("JETPING_PRICE_PROVIDER", "mock").strip().lower(),
        travelpayouts_token=os.getenv("TRAVELPAYOUTS_TOKEN", "").strip() or None,
        currency=os.getenv("JETPING_CURRENCY", "rub").strip().lower(),
        market=os.getenv("JETPING_MARKET", "ru").strip().lower(),
    )
