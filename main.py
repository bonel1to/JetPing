from __future__ import annotations

import asyncio
import logging

from app.bot import create_application
from app.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    application = create_application(settings)

    # Python 3.14 no longer provides a default event loop in the main thread.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
