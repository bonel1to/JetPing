from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

LOGGER = logging.getLogger(__name__)
UI_MESSAGE_IDS_BY_CHAT_KEY = "ui_message_ids_by_chat"

AIRLINE_NAMES = {
    "DP": "Победа",
    "SU": "Аэрофлот",
    "S7": "S7 Airlines",
    "U6": "Уральские авиалинии",
    "UT": "ЮТэйр",
    "FV": "Россия",
    "5N": "Smartavia",
    "N4": "Nordwind Airlines",
    "WZ": "Red Wings",
    "YC": "Ямал",
    "IO": "ИрАэро",
    "A4": "Азимут",
    "I8": "Ижавиа",
    "D2": "Северсталь",
    "B2": "Белавиа",
    "TK": "Turkish Airlines",
    "PC": "Pegasus Airlines",
    "EK": "Emirates",
    "EY": "Etihad Airways",
    "QR": "Qatar Airways",
    "HY": "Uzbekistan Airways",
    "J2": "Azerbaijan Airlines",
    "KC": "Air Astana",
    "CZ": "China Southern Airlines",
    "CA": "Air China",
    "MU": "China Eastern Airlines",
}


def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Вернуться в меню", callback_data="menu:back")]])


def format_airline_name(code: str | None) -> str | None:
    if not code:
        return None

    normalized = code.strip().upper()
    if not normalized:
        return None

    return AIRLINE_NAMES.get(normalized, normalized)

async def delete_tracked_ui_messages(
    bot: object,
    bot_data: dict,
    chat_id: int,
    extra_message_ids: list[int] | None = None,
) -> None:
    storage = bot_data.setdefault(UI_MESSAGE_IDS_BY_CHAT_KEY, {})
    message_ids = list(storage.get(chat_id, []))
    if extra_message_ids:
        message_ids.extend(extra_message_ids)

    storage[chat_id] = []
    seen: set[int] = set()
    for message_id in message_ids:
        if message_id in seen:
            continue
        seen.add(message_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            LOGGER.debug("Failed to delete UI message %s", message_id, exc_info=True)


def track_ui_message(bot_data: dict, chat_id: int, message_id: int) -> None:
    storage = bot_data.setdefault(UI_MESSAGE_IDS_BY_CHAT_KEY, {})
    storage[chat_id] = [message_id]

