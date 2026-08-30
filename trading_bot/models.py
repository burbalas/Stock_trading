from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Candle:
    date: date
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    confidence: float
    reason: str


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: int
    price: float
    reason: str
    client_order_id: str | None = None


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    average_cost: float = 0.0

    def market_value(self, price: float) -> float:
        return self.quantity * price


@dataclass(frozen=True)
class Fill:
    date: date
    symbol: str
    side: Side
    quantity: int
    price: float
    value: float
    reason: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    date: date
    cash: float
    positions_value: float
    equity: float
