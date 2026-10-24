from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite


@dataclass(frozen=True)
class BotConfig:
    symbols: list[str] = field(default_factory=lambda: ["AAPL", "MSFT"])
    starting_cash: float = 10_000.0
    max_position_pct: float = 0.25
    max_order_pct: float = 0.10
    max_open_positions: int = 10
    max_daily_orders: int = 5
    min_trade_value: float = 25.0
    target_cash_pct: float = 0.05
    stop_loss_pct: float = 0.08
    trailing_stop_pct: float = 0.05
    slippage_bps: float = 5.0
    commission_per_trade: float = 0.0
    max_drawdown_stop_pct: float = 0.20
    min_confidence: float = 0.55
    short_window: int = 10
    long_window: int = 30
    allow_short_selling: bool = False
    allow_new_buys: bool = True
    allow_position_adds: bool = False
    buy_cooldown_days: int = 3

    def validate(self) -> None:
        if not isinstance(self.symbols, list) or not self.symbols:
            raise ValueError("At least one symbol is required.")
        if any(not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip() for symbol in self.symbols):
            raise ValueError("symbols must contain nonempty ticker strings without surrounding whitespace.")
        if len({symbol.upper() for symbol in self.symbols}) != len(self.symbols):
            raise ValueError("symbols must be unique (case insensitive).")
        for name in (
            "starting_cash", "max_position_pct", "max_order_pct", "min_trade_value",
            "target_cash_pct", "stop_loss_pct", "trailing_stop_pct", "slippage_bps",
            "commission_per_trade", "max_drawdown_stop_pct", "min_confidence",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number.")
        for name in ("max_open_positions", "max_daily_orders", "short_window", "long_window", "buy_cooldown_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer.")
        for name in ("allow_short_selling", "allow_new_buys", "allow_position_adds"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean.")
        if self.allow_short_selling:
            raise ValueError("Short selling is not supported by this long-only bot.")
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive.")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max_position_pct must be between 0 and 1.")
        if not 0 < self.max_order_pct <= 1:
            raise ValueError("max_order_pct must be between 0 and 1.")
        if self.max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive.")
        if self.max_daily_orders <= 0:
            raise ValueError("max_daily_orders must be positive.")
        if self.min_trade_value < 0:
            raise ValueError("min_trade_value cannot be negative.")
        if not 0 <= self.target_cash_pct < 1:
            raise ValueError("target_cash_pct must be between 0 and 1.")
        if not 0 < self.stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be between 0 and 1.")
        if not 0 < self.trailing_stop_pct < 1:
            raise ValueError("trailing_stop_pct must be between 0 and 1.")
        if not 0 <= self.slippage_bps < 10_000:
            raise ValueError("slippage_bps must be between 0 (inclusive) and 10000 (exclusive).")
        if self.commission_per_trade < 0:
            raise ValueError("commission_per_trade cannot be negative.")
        if not 0 < self.max_drawdown_stop_pct < 1:
            raise ValueError("max_drawdown_stop_pct must be between 0 and 1.")
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1.")
        if self.short_window <= 0 or self.long_window <= 0:
            raise ValueError("moving average windows must be positive.")
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be smaller than long_window.")
        if self.buy_cooldown_days < 0:
            raise ValueError("buy_cooldown_days cannot be negative.")
