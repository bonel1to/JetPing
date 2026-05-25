from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import requests



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
    raise PriceProviderError(f"Unknown price provider: {name}")
