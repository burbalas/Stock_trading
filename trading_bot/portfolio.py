from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import BotConfig
from .models import Candle, Order, Signal, Side


@dataclass(frozen=True)
class OrderPlan:
    orders: list[Order]
    rejected: list[str]


class PortfolioManager:
    """Converts strategy/advisor signals into risk-checked orders."""

    def __init__(self, config: BotConfig) -> None:
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
        tradable = [
            signal
            for signal in signals
            if signal.side is not Side.HOLD and signal.confidence >= self.config.min_confidence
        ]

        sell_orders = self._sell_orders(tradable, prices, broker)
        for order in sell_orders:
            if len(orders) >= self.config.max_daily_orders:
                rejected.append(f"{order.symbol}: max daily orders reached before sell")
                break
            orders.append(order)

        open_positions = sum(1 for position in broker.positions.values() if position.quantity > 0)
        remaining_cash = max(0.0, broker.cash - equity * self.config.target_cash_pct)
        buy_signals = sorted(
            [signal for signal in tradable if signal.side is Side.BUY],
            key=lambda signal: signal.confidence,
            reverse=True,
        )

        for signal in buy_signals:
            if not self.config.allow_new_buys:
                rejected.append(f"{signal.symbol}: new buys disabled")
                continue
            if len(orders) >= self.config.max_daily_orders:
                rejected.append(f"{signal.symbol}: max daily orders reached")
                continue
            if signal.symbol not in prices:
                rejected.append(f"{signal.symbol}: missing price")
                continue

            price = prices[signal.symbol]
            position = broker.positions.get(signal.symbol)
            current_quantity = position.quantity if position else 0
            current_value = current_quantity * price
            is_new_position = current_quantity <= 0

            if not is_new_position and not self.config.allow_position_adds:
                rejected.append(f"{signal.symbol}: already held")
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
            order_value = min(equity * self.config.max_order_pct, remaining_cash, remaining_position_room)

            if order_value < self.config.min_trade_value:
                rejected.append(f"{signal.symbol}: order value below minimum")
                continue

            quantity = int(order_value // price)
            if quantity <= 0:
                rejected.append(f"{signal.symbol}: quantity rounded to zero")
                continue

            value = quantity * price
            orders.append(Order(signal.symbol, Side.BUY, quantity, price, signal.reason))
            remaining_cash -= value
            if is_new_position:
                open_positions += 1

        return OrderPlan(orders=orders, rejected=rejected)

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
        return 0 <= days_since_exit <= self.config.buy_cooldown_days

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
            if price is None or position.quantity <= 0 or position.average_cost <= 0:
                continue
            fixed_stop = position.average_cost * (1 - self.config.stop_loss_pct)
            trailing_high = position_highs.get(symbol, 0.0)
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
            if candle is None or position.quantity <= 0 or position.average_cost <= 0:
                continue
            fixed_stop = position.average_cost * (1 - self.config.stop_loss_pct)
            previous_high = position_highs.get(symbol, position.average_cost)
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
            orders.append(Order(signal.symbol, Side.SELL, position.quantity, price, signal.reason))
        return orders
