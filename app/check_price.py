from __future__ import annotations

import argparse
from datetime import date

from app.config import load_settings
from app.price_provider import FlightSearch, PriceProviderError, build_price_provider


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a flight price through the configured provider.")
    parser.add_argument("origin", help="Origin IATA code, for example MOW")
    parser.add_argument("destination", help="Destination IATA code, for example LED")
    parser.add_argument("departure_date", help="Departure date in YYYY-MM-DD format")
    parser.add_argument("--return-date", help="Return date in YYYY-MM-DD format")
    args = parser.parse_args()

    settings = load_settings(require_telegram_token=False)
    provider = build_price_provider(
        name=settings.price_provider,
        token=settings.travelpayouts_token,
        currency=settings.currency,
        market=settings.market,
    )

    search = FlightSearch(
        origin=args.origin.upper(),
        destination=args.destination.upper(),
        departure_date=date.fromisoformat(args.departure_date),
        return_date=date.fromisoformat(args.return_date) if args.return_date else None,
    )

    try:
        quote = provider.get_lowest_price(search)
    except PriceProviderError as exc:
        raise SystemExit(f"Price check failed: {exc}") from exc

    dates = quote.departure_date.isoformat()
    if quote.return_date:
        dates += f" - {quote.return_date.isoformat()}"

    print(f"Service: {quote.service}")
    print(f"Route: {quote.origin} -> {quote.destination}")
    print(f"Dates: {dates}")
    print(f"Price: {quote.price} {quote.currency.upper()}")
    if quote.airline:
        print(f"Airline: {quote.airline}")
    if quote.link:
        print(f"Link: {quote.link}")


if __name__ == "__main__":
    main()
