from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from .models import Candle


class MarketDataProvider(Protocol):
    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        """Return candles keyed by symbol, sorted by date."""


def validate_market_data(data: dict[str, list[Candle]], symbols: list[str] | None = None) -> dict[str, list[Candle]]:
    """Normalize and reject ambiguous or invalid bars before simulating trades."""
    normalized: dict[str, list[Candle]] = {}
    for key, rows in data.items():
        symbol = key.strip().upper()
        if not symbol or symbol in normalized:
            raise ValueError(f"Duplicate or empty market-data symbol: {key!r}")
        normalized[symbol] = []
        seen: set[date] = set()
        for candle in sorted(rows, key=lambda item: item.date):
            if candle.symbol.strip().upper() != symbol:
                raise ValueError(f"Candle symbol does not match market-data key {symbol}.")
            if candle.date in seen:
                raise ValueError(f"Duplicate candle for {symbol} on {candle.date}.")
            seen.add(candle.date)
            prices = (candle.open, candle.high, candle.low, candle.close)
            if any(not math.isfinite(price) or price <= 0 for price in prices):
                raise ValueError(f"Invalid price for {symbol} on {candle.date}.")
            if not candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high:
                raise ValueError(f"Invalid OHLC range for {symbol} on {candle.date}.")
            if not math.isfinite(candle.volume) or candle.volume < 0 or int(candle.volume) != candle.volume:
                raise ValueError(f"Invalid volume for {symbol} on {candle.date}.")
            normalized[symbol].append(replace(candle, symbol=symbol))
    if symbols is not None:
        missing = [symbol.upper() for symbol in symbols if not normalized.get(symbol.upper())]
        if missing:
            raise ValueError(f"No market data for requested symbols: {', '.join(missing)}")
        normalized = {symbol.upper(): normalized[symbol.upper()] for symbol in symbols}
    return normalized


class StaticMarketDataProvider:
    def __init__(self, data: dict[str, list[Candle]]) -> None:
        self.data = validate_market_data(data)

    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        return {
            symbol.upper(): list(self.data.get(symbol.upper(), []))
            for symbol in symbols
        }


class CsvMarketDataProvider:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        wanted = {symbol.upper() for symbol in symbols}
        candles: dict[str, list[Candle]] = defaultdict(list)

        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            required = {"date", "symbol", "open", "high", "low", "close", "volume"}
            missing = required.difference(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

            for row in reader:
                symbol = row["symbol"].upper()
                if symbol not in wanted:
                    continue
                candles[symbol].append(
                    Candle(
                        date=date.fromisoformat(row["date"]),
                        symbol=symbol,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row["volume"])),
                    )
                )

        return validate_market_data(dict(candles), symbols)


class DemoMarketDataProvider:
    def __init__(self, days: int = 180, seed: int = 7) -> None:
        self.days = days
        self.seed = seed

    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        rng = random.Random(self.seed)
        today = date.today()
        start = today - timedelta(days=self.days)
        data: dict[str, list[Candle]] = {}

        for offset, symbol in enumerate(symbols):
            base = 80 + offset * 35 + rng.random() * 20
            drift = 0.0008 + offset * 0.0002
            rows: list[Candle] = []

            for index in range(self.days):
                current_date = start + timedelta(days=index)
                if current_date.weekday() >= 5:
                    continue

                seasonal = math.sin(index / 12) * 0.012
                noise = rng.gauss(0, 0.015)
                close = max(1.0, base * (1 + drift + seasonal + noise))
                open_price = base
                high = max(open_price, close) * (1 + rng.random() * 0.01)
                low = min(open_price, close) * (1 - rng.random() * 0.01)
                volume = int(1_000_000 + rng.random() * 2_000_000)

                rows.append(
                    Candle(
                        date=current_date,
                        symbol=symbol.upper(),
                        open=round(open_price, 2),
                        high=round(high, 2),
                        low=round(low, 2),
                        close=round(close, 2),
                        volume=volume,
                    )
                )
                base = close

            data[symbol.upper()] = rows

        return data
