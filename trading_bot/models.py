from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from math import isfinite


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

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip() or self.symbol != self.symbol.strip():
            raise ValueError("Signal symbol must be a nonempty ticker without surrounding whitespace.")
        object.__setattr__(self, "symbol", self.symbol.upper())
        if not isinstance(self.side, Side):
            raise ValueError("Signal side must be a Side value.")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("Signal confidence must be a finite number between 0 and 1.")


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: int
    price: float
    reason: str
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip() or self.symbol != self.symbol.strip():
            raise ValueError("Order symbol must be a nonempty ticker without surrounding whitespace.")
        object.__setattr__(self, "symbol", self.symbol.upper())
        if self.side is not Side.BUY and self.side is not Side.SELL:
            raise ValueError("Order side must be Side.BUY or Side.SELL.")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("Order quantity must be a positive whole number of shares.")
        if (
            isinstance(self.price, bool)
            or not isinstance(self.price, (int, float))
            or not isfinite(self.price)
            or self.price <= 0
        ):
            raise ValueError("Order price must be a finite positive number.")


@dataclass
class Position:
    symbol: str
    quantity: int | float = 0
    average_cost: float = 0.0
    # Preserve the broker's decimal representation for reconciliation. Numeric
    # quantity remains available to portfolio/risk calculations.
    broker_quantity: str | None = field(default=None, compare=False)

    def market_value(self, price: float) -> float:
        return self.quantity * price


@dataclass(frozen=True)
class Fill:
    date: date
    symbol: str
    side: Side
    quantity: int | float
    price: float
    value: float
    reason: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    date: date
    cash: float
    positions_value: float
    equity: float
