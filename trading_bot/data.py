from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from .models import Candle


class MarketDataProvider(Protocol):
    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        """Return candles keyed by symbol, sorted by date."""


class StaticMarketDataProvider:
    def __init__(self, data: dict[str, list[Candle]]) -> None:
        self.data = {
            symbol.upper(): sorted(rows, key=lambda candle: candle.date)
            for symbol, rows in data.items()
        }

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

        return {symbol: sorted(rows, key=lambda candle: candle.date) for symbol, rows in candles.items()}


class DemoMarketDataProvider:
    def __init__(self, days: int = 180, seed: int = 7) -> None:
        self.days = days
        self.seed = seed

    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        random.seed(self.seed)
        today = date.today()
        start = today - timedelta(days=self.days)
        data: dict[str, list[Candle]] = {}

        for offset, symbol in enumerate(symbols):
            base = 80 + offset * 35 + random.random() * 20
            drift = 0.0008 + offset * 0.0002
            rows: list[Candle] = []

            for index in range(self.days):
                current_date = start + timedelta(days=index)
                if current_date.weekday() >= 5:
                    continue

                seasonal = math.sin(index / 12) * 0.012
                noise = random.gauss(0, 0.015)
                close = max(1.0, base * (1 + drift + seasonal + noise))
                open_price = base
                high = max(open_price, close) * (1 + random.random() * 0.01)
                low = min(open_price, close) * (1 - random.random() * 0.01)
                volume = int(1_000_000 + random.random() * 2_000_000)

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
