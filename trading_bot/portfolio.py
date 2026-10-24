from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite

from .config import BotConfig
from .models import Candle, Order, Signal, Side


@dataclass(frozen=True)
class OrderPlan:
    orders: list[Order]
    rejected: list[str]


class PortfolioManager:
    """Converts strategy/advisor signals into risk-checked orders."""

    def __init__(self, config: BotConfig) -> None:
        config.validate()
        self.config = config

    def build_order_plan(
        self,
        signals: list[Signal],
        prices: dict[str, float],
        broker,
        equity: float,
        as_of_date: date | None = None,
        recent_exit_dates: dict[str, date] | None = None,
    ) -> OrderPlan:
        orders: list[Order] = []
        rejected: list[str] = []
        # At most one order per symbol. An exit wins over a conflicting entry.
        by_symbol: dict[str, Signal] = {}
        for signal in signals:
            signal.__post_init__()
            if signal.side is Side.HOLD or signal.confidence < self.config.min_confidence:
                continue
            previous = by_symbol.get(signal.symbol)
            if previous is None:
                by_symbol[signal.symbol] = signal
                continue
            rejected.append(f"{signal.symbol}: duplicate or conflicting signal ignored")
            if (signal.side is Side.SELL and previous.side is Side.BUY) or (
                signal.side is previous.side and signal.confidence > previous.confidence
            ):
                by_symbol[signal.symbol] = signal
        tradable = list(by_symbol.values())

        # Exit orders reduce exposure and must remain available when an entry cap is reached.
        orders.extend(self._sell_orders(tradable, prices, broker))

        open_positions = sum(1 for position in broker.positions.values() if position.quantity > 0)
        account_valid = self._valid_price(equity) and self._finite_number(broker.cash) and broker.cash >= 0
        positions_valid = all(
            isinstance(position.quantity, int) and not isinstance(position.quantity, bool)
            and position.quantity >= 0
            and (position.quantity == 0 or self._valid_price(position.average_cost))
            for position in broker.positions.values()
        )
        marks_complete = all(
            self._valid_price(prices.get(symbol))
            for symbol, position in broker.positions.items() if position.quantity > 0
        )
        remaining_cash = broker.cash if account_valid else 0.0
        buy_count = 0
        configured_symbols = {symbol.upper() for symbol in self.config.symbols}
        buy_signals = sorted(
            [signal for signal in tradable if signal.side is Side.BUY],
            key=lambda signal: signal.confidence,
            reverse=True,
        )

        for signal in buy_signals:
            if not self.config.allow_new_buys:
                rejected.append(f"{signal.symbol}: new buys disabled")
                continue
            if not account_valid or not positions_valid:
                rejected.append(f"{signal.symbol}: invalid account or unsupported position state")
                continue
            if not marks_complete:
                rejected.append(f"{signal.symbol}: missing or invalid held-position price")
                continue
            if signal.symbol.upper() not in configured_symbols:
                rejected.append(f"{signal.symbol}: symbol outside configured universe")
                continue
            if buy_count >= self.config.max_daily_orders:
                rejected.append(f"{signal.symbol}: max daily orders reached")
                continue
            if signal.symbol not in prices:
                rejected.append(f"{signal.symbol}: missing price")
                continue

            price = prices[signal.symbol]
            if not self._valid_price(price):
                rejected.append(f"{signal.symbol}: invalid price")
                continue
            position = broker.positions.get(signal.symbol)
            current_quantity = position.quantity if position else 0
            current_value = current_quantity * price
            is_new_position = current_quantity <= 0

            if not is_new_position and not self.config.allow_position_adds:
                rejected.append(f"{signal.symbol}: already held")
                continue
            if not is_new_position and price <= position.average_cost * (1 - self.config.stop_loss_pct):
                rejected.append(f"{signal.symbol}: position has reached its stop loss")
                continue

            last_exit = self._last_exit_date(signal.symbol, broker, recent_exit_dates)
            if is_new_position and self._buy_cooldown_active(as_of_date, last_exit):
                rejected.append(f"{signal.symbol}: buy cooldown after {last_exit.isoformat()} exit")
                continue

            if is_new_position and open_positions >= self.config.max_open_positions:
                rejected.append(f"{signal.symbol}: max open positions reached")
                continue

            max_position_value = equity * self.config.max_position_pct
            remaining_position_room = max(0.0, max_position_value - current_value)
            order_value = min(
                equity * self.config.max_order_pct,
                max(0.0, remaining_cash - equity * self.config.target_cash_pct),
                remaining_position_room,
            )

            if order_value < self.config.min_trade_value:
                rejected.append(f"{signal.symbol}: order value below minimum")
                continue

            # Reserve estimated execution costs for every order, including rounding effects.
            execution_price = price + price * (self.config.slippage_bps / 10_000)
            commission = self.config.commission_per_trade
            quantity = self.max_buy_quantity(price, equity, remaining_cash, current_quantity)
            if quantity <= 0:
                rejected.append(f"{signal.symbol}: quantity rounded to zero or rounded order value below minimum")
                continue

            orders.append(Order(signal.symbol, Side.BUY, quantity, price, signal.reason))
            remaining_cash -= quantity * execution_price + commission
            buy_count += 1
            if is_new_position:
                open_positions += 1

        return OrderPlan(orders=orders, rejected=rejected)

    def max_buy_quantity(self, price: float, equity: float, cash: float, current_quantity: int = 0) -> int:
        """Size a buy from fresh marks and raw cash, reserving configured costs and cash.

        Execution callers should also cap the result to the original requested quantity.
        Position-count, signal, cooldown and account-state checks remain the caller's job.
        """
        if (
            not self._valid_price(price) or not self._valid_price(equity)
            or not self._finite_number(cash) or cash < 0
            or isinstance(current_quantity, bool) or not isinstance(current_quantity, int)
            or current_quantity < 0
        ):
            return 0
        budget = min(
            equity * self.config.max_order_pct,
            max(0.0, cash - equity * self.config.target_cash_pct),
            max(0.0, equity * self.config.max_position_pct - current_quantity * price),
        )
        execution_price = price + price * (self.config.slippage_bps / 10_000)
        if not self._valid_price(execution_price):
            return 0
        quantity = int(max(0.0, budget - self.config.commission_per_trade) // execution_price)
        if quantity * execution_price + self.config.commission_per_trade > budget:
            quantity = max(0, quantity - 1)
        return quantity if quantity * price >= self.config.min_trade_value else 0

    @staticmethod
    def _finite_number(value) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)

    @classmethod
    def _valid_price(cls, value) -> bool:
        return cls._finite_number(value) and value > 0

    def _last_exit_date(self, symbol: str, broker, recent_exit_dates: dict[str, date] | None) -> date | None:
        dates: list[date] = []
        if recent_exit_dates and symbol in recent_exit_dates:
            dates.append(recent_exit_dates[symbol])
        for fill in getattr(broker, "fills", []):
            if fill.symbol == symbol and fill.side is Side.SELL:
                dates.append(fill.date)
        return max(dates) if dates else None

    def _buy_cooldown_active(self, as_of_date: date | None, last_exit: date | None) -> bool:
        if self.config.buy_cooldown_days <= 0 or as_of_date is None or last_exit is None:
            return False
        days_since_exit = (as_of_date - last_exit).days
        return days_since_exit <= self.config.buy_cooldown_days

    def stop_loss_orders(
        self,
        prices: dict[str, float],
        broker,
        position_highs: dict[str, float] | None = None,
    ) -> list[Order]:
        orders: list[Order] = []
        position_highs = position_highs or {}
        for symbol, position in broker.positions.items():
            price = prices.get(symbol)
            if price is None or position.quantity <= 0:
                continue
            if not self._valid_price(price) or not self._valid_price(position.average_cost):
                raise ValueError(f"{symbol}: invalid price or cost for stop evaluation.")
            fixed_stop = position.average_cost * (1 - self.config.stop_loss_pct)
            trailing_high = position_highs.get(symbol, 0.0)
            if not self._finite_number(trailing_high) or trailing_high < 0:
                raise ValueError(f"{symbol}: invalid trailing high.")
            trailing_stop = 0.0
            if trailing_high > position.average_cost:
                trailing_stop = trailing_high * (1 - self.config.trailing_stop_pct)

            stop_price = max(fixed_stop, trailing_stop)
            if price <= stop_price:
                reason = "trailing stop" if trailing_stop >= fixed_stop and trailing_stop > 0 else "stop loss"
                orders.append(Order(symbol, Side.SELL, position.quantity, price, reason))
        return orders

    def bar_stop_orders(
        self,
        candles: dict[str, Candle],
        broker,
        position_highs: dict[str, float],
    ) -> list[Order]:
        orders: list[Order] = []
        for symbol, position in broker.positions.items():
            candle = candles.get(symbol)
            if candle is None or position.quantity <= 0:
                continue
            fixed_stop = position.average_cost * (1 - self.config.stop_loss_pct)
            previous_high = position_highs.get(symbol, position.average_cost)
            if not all(self._valid_price(value) for value in (candle.open, candle.low, position.average_cost, previous_high)):
                raise ValueError(f"{symbol}: invalid price or trailing high for bar stop.")
            trailing_stop = 0.0
            if previous_high > position.average_cost:
                trailing_stop = previous_high * (1 - self.config.trailing_stop_pct)
            stop_price = max(fixed_stop, trailing_stop)
            if candle.open <= stop_price:
                execution_price = candle.open
            elif candle.low <= stop_price:
                execution_price = stop_price
            else:
                continue
            reason = "trailing stop" if trailing_stop >= fixed_stop and trailing_stop > 0 else "stop loss"
            orders.append(Order(symbol, Side.SELL, position.quantity, execution_price, reason))
        return orders

    def account_drawdown_orders(self, prices: dict[str, float], broker, account_drawdown: float) -> list[Order]:
        if not self._finite_number(account_drawdown) or account_drawdown < 0:
            raise ValueError("account_drawdown must be finite and nonnegative.")
        if account_drawdown < self.config.max_drawdown_stop_pct:
            return []
        return [
            Order(symbol, Side.SELL, position.quantity, prices[symbol], "account drawdown stop")
            for symbol, position in sorted(broker.positions.items())
            if position.quantity > 0 and symbol in prices
        ]

    def _sell_orders(self, signals: list[Signal], prices: dict[str, float], broker) -> list[Order]:
        orders: list[Order] = []
        sell_signals = sorted(
            [signal for signal in signals if signal.side is Side.SELL],
            key=lambda signal: signal.confidence,
            reverse=True,
        )
        for signal in sell_signals:
            price = prices.get(signal.symbol)
            position = broker.positions.get(signal.symbol)
            if price is None or not position or position.quantity <= 0:
                continue
            if not self._valid_price(price):
                raise ValueError(f"{signal.symbol}: invalid price for sell order.")
            orders.append(Order(signal.symbol, Side.SELL, position.quantity, price, signal.reason))
        return orders
