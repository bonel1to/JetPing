from __future__ import annotations

import asyncio
import logging

from app.bot import create_application # type: ignore [attr-defined]
from app.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Python 3.14 no longer provides a default event loop in the main thread.
    asyncio.set_event_loop(asyncio.new_event_loop())
    settings = load_settings()
    # Initialize database
    from app.db import init_db
    init_db(settings.database_path)
    application = create_application(settings)

    logging.info("Бот успешно запущен и готов к работе! Нажмите Ctrl+C для остановки.")
    application.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
