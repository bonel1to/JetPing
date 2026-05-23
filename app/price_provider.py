from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import requests

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlightSearch:
    origin: str
    destination: str
    departure_date: date
    return_date: date | None = None


@dataclass(frozen=True)
class PriceQuote:
    service: str
    origin: str
    destination: str
    departure_date: date
    return_date: date | None
    price: int
    currency: str
    airline: str | None = None
    link: str | None = None


class PriceProviderError(Exception):
    """Raised when a flight price provider cannot return a usable quote."""


class PriceProvider(Protocol):
    def get_lowest_price(self, search: FlightSearch) -> PriceQuote:
        ...


class MockPriceProvider:
    """Local deterministic provider for development without paid/external API access."""

    def __init__(self, currency: str = "rub") -> None:
        self.currency = currency

    def get_lowest_price(self, search: FlightSearch) -> PriceQuote:
        seed = f"{search.origin}:{search.destination}:{search.departure_date}:{search.return_date}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        price = 7000 + (int(digest[:6], 16) % 23000)

        return PriceQuote(
            service="Mock provider",
            origin=search.origin,
            destination=search.destination,
            departure_date=search.departure_date,
            return_date=search.return_date,
            price=price,
            currency=self.currency,
            airline="JP",
            link=None,
        )


class TravelpayoutsPriceProvider:
    endpoint = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

    def __init__(
        self,
        token: str | None,
        currency: str = "rub",
        market: str = "ru",
        timeout: tuple[int, int] = (10, 45),
        retries: int = 2,
    ) -> None:
        if not token:
            raise PriceProviderError("TRAVELPAYOUTS_TOKEN is required for travelpayouts provider.")
        self.token = token
        self.currency = currency
        self.market = market
        self.timeout = timeout
        self.retries = retries

    def get_lowest_price(self, search: FlightSearch) -> PriceQuote:
        params: dict[str, str | int] = {
            "origin": search.origin,
            "destination": search.destination,
            "departure_at": search.departure_date.isoformat(),
            "currency": self.currency,
            "market": self.market,
            "direct": "false",
            "unique": "false",
            "one_way": "false" if search.return_date else "true",
            "sorting": "price",
            "limit": 1,
            "page": 1,
        }

        if search.return_date:
            params["return_at"] = search.return_date.isoformat()

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "X-Access-Token": self.token,
        }

        response = self._get(params=params, headers=headers)

        try:
            payload = response.json()
        except ValueError as exc:
            raise PriceProviderError("Price source returned invalid JSON.") from exc

        if not payload.get("success", True):
            raise PriceProviderError(str(payload.get("error") or "Price source returned an error."))

        items = payload.get("data") or []
        if not items:
            raise PriceProviderError("No tickets found for this route and date.")
        if not isinstance(items, list):
            raise PriceProviderError("Price source returned an unexpected data format.")

        first = items[0]
        raw_price = first.get("price")
        if raw_price is None:
            raise PriceProviderError("Price source response does not contain a price.")

        link = first.get("link")
        if link and link.startswith("/"):
            link = f"https://www.aviasales.ru{link}"

        return PriceQuote(
            service="Aviasales / Travelpayouts",
            origin=first.get("origin") or search.origin,
            destination=first.get("destination") or search.destination,
            departure_date=parse_api_date(first.get("departure_at")) or search.departure_date,
            return_date=parse_api_date(first.get("return_at")) or search.return_date,
            price=int(raw_price),
            currency=payload.get("currency") or self.currency,
            airline=first.get("airline"),
            link=link,
        )

    def _get(self, params: dict[str, str | int], headers: dict[str, str]) -> requests.Response:
        last_error: requests.RequestException | None = None

        for attempt in range(self.retries + 1):
            try:
                response = requests.get(
                    self.endpoint,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response
            except requests.Timeout as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise PriceProviderError(
                    "Travelpayouts API timed out. Check internet access, VPN/proxy, "
                    "or try again later."
                ) from exc
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else "unknown"
                if status in {401, 403}:
                    raise PriceProviderError("Travelpayouts rejected the API token or access is not enabled.") from exc
                if status == 429:
                    raise PriceProviderError("Travelpayouts rate limit exceeded. Try again later.") from exc
                raise PriceProviderError(f"Travelpayouts API returned HTTP {status}.") from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise PriceProviderError(
                    "Could not connect to Travelpayouts API. Check internet access, VPN/proxy, "
                    "or try again later."
                ) from exc

        raise PriceProviderError(f"Travelpayouts API request failed: {last_error}")


class AeroflotProvider:
    """
    Парсер для сайта авиакомпании Аэрофлот.
    Обращается к внутреннему API бронирования.
    """
    def __init__(self, currency: str = "rub") -> None:
        self.currency = currency.lower()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.aeroflot.ru",
            "Referer": "https://www.aeroflot.ru/sb/app/ru-ru",
        }

    def get_lowest_price(self, search: FlightSearch) -> PriceQuote:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError:
            raise PriceProviderError("Для парсинга Аэрофлота нужно установить playwright: "
                                     "pip install playwright && playwright install")

        api_url = "https://www.aeroflot.ru/sb/api/app/ru-ru/search"
        
        payload = {
            "routes": [
                {
                    "origin": search.origin,
                    "destination": search.destination,
                    "departureDate": search.departure_date.strftime("%Y-%m-%d")
                }
            ],
            "passengers": {"ADT": 1, "CHD": 0, "INF": 0},
            "cabinClass": "ECONOMY",
        }
        
        if search.return_date:
            payload["routes"].append({
                "origin": search.destination,
                "destination": search.origin,
                "departureDate": search.return_date.strftime("%Y-%m-%d")
            })

        try:
            with sync_playwright() as p:
                # Запускаем скрытый браузер
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.headers["User-Agent"],
                    viewport={"width": 1920, "height": 1080}
                )
                page = context.new_page()
                
                # 1. Заходим на главную, чтобы отработали скрипты защиты и выдались куки
                page.goto("https://www.aeroflot.ru/sb/app/ru-ru", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000) # Даем защите 3 секунды на валидацию сессии
                
                # 2. Делаем API-запрос с полученными куками и токенами
                response = context.request.post(
                    api_url,
                    data=payload,
                    headers={"Accept": "application/json, text/plain, */*"}
                )
                
                if not response.ok:
                    raise PriceProviderError(f"Playwright API Error: {response.status} {response.status_text}")
                    
                try:
                    data = response.json()
                except Exception as json_err:
                    body_snippet = response.text()[:300]
                    LOGGER.error("Aeroflot returned non-JSON (possibly a captcha/block): %s", body_snippet)
                    raise PriceProviderError("Аэрофлот вернул страницу защиты вместо данных.") from json_err
                
                recommendations = data.get("data", {}).get("recommendations", [])
                if not recommendations:
                    raise PriceProviderError("Аэрофлот: Билеты на эти даты не найдены.")

                min_price = min(rec["price"]["amount"] for rec in recommendations)
                
                return PriceQuote(
                    service="Aeroflot Airlines",
                    origin=search.origin,
                    destination=search.destination,
                    departure_date=search.departure_date,
                    return_date=search.return_date,
                    price=int(min_price),
                    currency=self.currency,
                    airline="Aeroflot",
                    link=f"https://www.aeroflot.ru/sb/app/ru-ru#/search?route={search.origin}-{search.destination}"
                )

        except PlaywrightTimeoutError as e:
            raise PriceProviderError("Таймаут при попытке обойти защиту Аэрофлота.")
        except Exception as e:
            LOGGER.error("Aeroflot parse error. Data format changed? Error: %s", e)
            raise PriceProviderError("Не удалось распарсить ответ Аэрофлота.")


class S7Provider:
    """
    Парсер для сайта авиакомпании S7 Airlines.
    """
    def __init__(self, currency: str = "rub") -> None:
        self.currency = currency.lower()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "application/json",
            "x-application-client": "web",
        }

    def get_lowest_price(self, search: FlightSearch) -> PriceQuote:
        api_url = "https://wftc.s7.ru/flight-search/v1/search"
        
        params: dict[str, str | int] = {
            "origin": search.origin,
            "destination": search.destination,
            "departureDate": search.departure_date.strftime("%Y-%m-%d"),
            "adults": 1,
            "children": 0,
            "infants": 0,
            "cabin": "ECONOMY"
        }
        
        if search.return_date:
            params["returnDate"] = search.return_date.strftime("%Y-%m-%d")

        try:
            response = requests.get(
                api_url,
                params=params,
                headers=self.headers,
                timeout=15,
                proxies={"http": None, "https": None}  # Обход нерабочих системных прокси
            )
            if response.status_code == 403:
                raise PriceProviderError("S7 заблокировал запрос (сработала защита от ботов).")
                
            response.raise_for_status()
            data = response.json()
            
            offers = data.get("offers", [])
            if not offers:
                raise PriceProviderError("S7: Рейсы не найдены.")
                
            min_price = min(offer["price"]["total"] for offer in offers)

            return PriceQuote(
                service="S7 Airlines",
                origin=search.origin,
                destination=search.destination,
                departure_date=search.departure_date,
                return_date=search.return_date,
                price=int(min_price),
                currency=self.currency,
                airline="S7",
                link=f"https://www.s7.ru/ru/avia/bilet-{search.origin}-{search.destination}/"
            )

        except requests.RequestException as e:
            LOGGER.error("S7 API Error: %s", e)
            raise PriceProviderError(f"Ошибка соединения с S7: {e}")
        except (KeyError, ValueError) as e:
            LOGGER.error("S7 parse error: %s", e)
            raise PriceProviderError("Не удалось распарсить ответ S7.")


def parse_api_date(value: str | None) -> date | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def build_price_provider(name: str, token: str | None, currency: str, market: str = "ru") -> PriceProvider:
    if name == "mock":
        return MockPriceProvider(currency=currency)
    if name == "travelpayouts":
        return TravelpayoutsPriceProvider(token=token, currency=currency, market=market)
    if name == "aeroflot":
        return AeroflotProvider(currency=currency)
    if name == "s7":
        return S7Provider(currency=currency)
    raise PriceProviderError(f"Unknown price provider: {name}")
