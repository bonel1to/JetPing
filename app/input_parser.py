from __future__ import annotations

import logging
import re
from datetime import date

import requests

LOGGER = logging.getLogger(__name__)
AUTOCOMPLETE_ENDPOINT = "https://autocomplete.travelpayouts.com/places2"

CITY_ALIASES = {
    "MOW": ["mow", "москва", "москвы", "moscow"],
    "LED": ["led", "санкт петербург", "санкт-петербург", "петербург", "питер", "спб", "saint petersburg", "st petersburg"],
    "AER": ["aer", "сочи", "адлер", "sochi", "adler"],
    "KZN": ["kzn", "казань", "kazan"],
    "SVX": ["svx", "екатеринбург", "екб", "yekaterinburg", "ekaterinburg"],
    "OVB": ["ovb", "новосибирск", "novosibirsk"],
    "KUF": ["kuf", "самара", "samara"],
    "UFA": ["ufa", "уфа"],
    "ROV": ["rov", "ростов", "ростов на дону", "ростов-на-дону", "rostov"],
    "KRR": ["krr", "краснодар", "krasnodar"],
    "VOG": ["vog", "волгоград", "volgograd"],
    "GOJ": ["goj", "нижний новгород", "нижний", "nizhny novgorod"],
    "CEK": ["cek", "челябинск", "chelyabinsk"],
    "OMS": ["oms", "омск", "omsk"],
    "PEE": ["pee", "пермь", "perm"],
    "MRV": ["mrv", "минеральные воды", "минводы", "mineralnye vody"],
    "KGD": ["kgd", "калининград", "kaliningrad"],
    "VVO": ["vvo", "владивосток", "vladivostok"],
    "IKT": ["ikt", "иркутск", "irkutsk"],
    "KJA": ["kja", "красноярск", "krasnoyarsk"],
    "TJM": ["tjm", "тюмень", "tyumen"],
    "MCX": ["mcx", "махачкала", "makhachkala"],
    "MSQ": ["msq", "минск", "minsk"],
    "IST": ["ist", "стамбул", "istanbul"],
    "AYT": ["ayt", "анталья", "antalya"],
    "DXB": ["dxb", "дубай", "dubai"],
    "EVN": ["evn", "ереван", "yerevan"],
    "TBS": ["tbs", "тбилиси", "tbilisi"],
    "GYD": ["gyd", "баку", "baku"],
}

CITY_NAMES = {
    "MOW": "Москва",
    "LED": "Санкт-Петербург",
    "AER": "Сочи",
    "KZN": "Казань",
    "SVX": "Екатеринбург",
    "OVB": "Новосибирск",
    "KUF": "Самара",
    "UFA": "Уфа",
    "ROV": "Ростов-на-Дону",
    "KRR": "Краснодар",
    "VOG": "Волгоград",
    "GOJ": "Нижний Новгород",
    "CEK": "Челябинск",
    "OMS": "Омск",
    "PEE": "Пермь",
    "MRV": "Минеральные Воды",
    "KGD": "Калининград",
    "VVO": "Владивосток",
    "IKT": "Иркутск",
    "KJA": "Красноярск",
    "TJM": "Тюмень",
    "MCX": "Махачкала",
    "MSQ": "Минск",
    "IST": "Стамбул",
    "AYT": "Анталья",
    "DXB": "Дубай",
    "EVN": "Ереван",
    "TBS": "Тбилиси",
    "GYD": "Баку",
}

MONTHS = {
    "января": 1,
    "январь": 1,
    "янв": 1,
    "февраля": 2,
    "февраль": 2,
    "фев": 2,
    "марта": 3,
    "март": 3,
    "мар": 3,
    "апреля": 4,
    "апрель": 4,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июн": 6,
    "июля": 7,
    "июль": 7,
    "июл": 7,
    "августа": 8,
    "август": 8,
    "авг": 8,
    "сентября": 9,
    "сентябрь": 9,
    "сен": 9,
    "сент": 9,
    "октября": 10,
    "октябрь": 10,
    "окт": 10,
    "ноября": 11,
    "ноябрь": 11,
    "ноя": 11,
    "декабря": 12,
    "декабрь": 12,
    "дек": 12,
}

def resolve_city_code(value: str) -> str | None:
    local_code = parse_city_code(value)
    if local_code:
        return local_code

    return fetch_city_code_from_autocomplete(value)


def fetch_city_code_from_autocomplete(value: str) -> str | None:
    term = value.strip()
    if len(term) < 2:
        return None

    params = [
        ("locale", "ru"),
        ("types[]", "city"),
        ("types[]", "airport"),
        ("term", term),
    ]

    try:
        response = requests.get(AUTOCOMPLETE_ENDPOINT, params=params, timeout=(3, 8))
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        LOGGER.warning("Failed to resolve city through Travelpayouts autocomplete", exc_info=True)
        return None

    if not isinstance(payload, list):
        return None

    for item in payload:
        if not isinstance(item, dict):
            continue
        code = extract_location_code(item)
        if not code:
            continue
        remember_location_name(code, item)
        return code

    return None


def extract_location_code(item: dict) -> str | None:
    raw_code = item.get("code") or item.get("city_code")
    if not isinstance(raw_code, str):
        return None

    code = raw_code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3}", code):
        return None
    return code


def remember_location_name(code: str, item: dict) -> None:
    name = item.get("name") or item.get("city_name")
    country = item.get("country_name")
    if not isinstance(name, str) or not name.strip():
        return

    label = name.strip()
    if isinstance(country, str) and country.strip() and country.strip() not in label:
        label = f"{label}, {country.strip()}"
    CITY_NAMES[code] = label

def parse_city_code(value: str) -> str | None:
    normalized = normalize_text(value)
    if re.fullmatch(r"[a-z]{3}", normalized):
        return normalized.upper()

    for code, aliases in CITY_ALIASES.items():
        if normalized in aliases:
            return code
    return None


def format_city(code: str) -> str:
    upper_code = code.upper()
    name = CITY_NAMES.get(upper_code)
    if not name:
        return upper_code
    return f"{name} ({upper_code})"


def format_route(origin: str, destination: str) -> str:
    return f"{format_city(origin)} -> {format_city(destination)}"


def parse_user_date(value: str, today: date | None = None) -> date | None:
    text = value.strip().lower()
    if not text:
        return None

    parsed = parse_iso_date(text) or parse_numeric_date(text) or parse_russian_date(text, today=today)
    return parsed


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_numeric_date(value: str) -> date | None:
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})", value.strip())
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3))
    if year < 100:
        year += 2000
    return build_date(year, month, day)


def parse_russian_date(value: str, today: date | None = None) -> date | None:
    normalized = normalize_text(value)
    match = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)\s*(\d{4})?", normalized)
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS.get(match.group(2))
    if month is None:
        return None

    current = today or date.today()
    year = int(match.group(3)) if match.group(3) else current.year
    parsed = build_date(year, month, day)
    if parsed is None:
        return None

    if not match.group(3) and parsed < current:
        parsed = build_date(year + 1, month, day)
    return parsed


def build_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_text(value: str) -> str:
    text = value.strip().lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()



