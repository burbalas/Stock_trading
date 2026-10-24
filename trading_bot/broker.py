from __future__ import annotations

from math import isfinite

from .models import Fill, Order, PortfolioSnapshot, Position, Side


class PaperBroker:
    def __init__(self, starting_cash: float, slippage_bps: float = 0.0, commission_per_trade: float = 0.0) -> None:
        for name, value in (
            ("starting_cash", starting_cash), ("slippage_bps", slippage_bps),
            ("commission_per_trade", commission_per_trade),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number.")
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive.")
        if not 0 <= slippage_bps < 10_000:
            raise ValueError("slippage_bps must be between 0 (inclusive) and 10000 (exclusive).")
        if commission_per_trade < 0:
            raise ValueError("commission_per_trade cannot be negative.")
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.slippage_bps = slippage_bps
        self.commission_per_trade = commission_per_trade

    def submit(self, order: Order, trade_date) -> Fill | None:
        order.__post_init__()
        if not isfinite(self.cash) or self.cash < 0:
            raise ValueError("Paper broker cash must be finite and nonnegative.")

        execution_price = self._execution_price(order)
        value = order.quantity * execution_price
        if not isfinite(value) or not isfinite(execution_price) or execution_price <= 0:
            raise ValueError("Order execution value must be finite and positive.")
        position = self.positions.get(order.symbol, Position(order.symbol))
        self._validate_position(position)

        if order.side is Side.BUY:
            total_cost = value + self.commission_per_trade
            if total_cost > self.cash:
                return None
            new_quantity = position.quantity + order.quantity
            position.average_cost = (
                (position.average_cost * position.quantity + value) / new_quantity
                if new_quantity
                else 0.0
            )
            position.quantity = new_quantity
            self.cash -= total_cost
        elif order.side is Side.SELL:
            sell_quantity = min(order.quantity, position.quantity)
            if sell_quantity <= 0:
                return None
            value = sell_quantity * execution_price
            proceeds = value - self.commission_per_trade
            if self.cash + proceeds < 0:
                return None
            position.quantity -= sell_quantity
            if position.quantity == 0:
                position.average_cost = 0.0
            self.cash += proceeds
            order = Order(order.symbol, order.side, sell_quantity, execution_price, order.reason, order.client_order_id)
        else:
            return None

        self.positions[order.symbol] = position
        fill = Fill(
            date=trade_date,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=execution_price,
            value=value,
            reason=order.reason,
        )
        self.fills.append(fill)
        return fill

    def _execution_price(self, order: Order) -> float:
        slip = self.slippage_bps / 10_000
        if order.side is Side.BUY:
            return order.price + order.price * slip
        if order.side is Side.SELL:
            return order.price - order.price * slip
        return order.price

    def snapshot(self, prices: dict[str, float], current_date) -> PortfolioSnapshot:
        if not isfinite(self.cash) or self.cash < 0:
            raise ValueError("Paper broker cash must be finite and nonnegative.")
        positions_value = 0.0
        for symbol, position in self.positions.items():
            self._validate_position(position)
            if position.quantity == 0:
                continue
            price = prices.get(symbol)
            if (
                isinstance(price, bool) or not isinstance(price, (int, float))
                or not isfinite(price) or price <= 0
            ):
                raise ValueError(f"Missing or invalid market price for held position {symbol}.")
            positions_value += position.market_value(price)
        if not isfinite(self.cash + positions_value):
            raise ValueError("Portfolio equity must be finite.")
        return PortfolioSnapshot(
            date=current_date,
            cash=self.cash,
            positions_value=positions_value,
            equity=self.cash + positions_value,
        )

    @staticmethod
    def _validate_position(position: Position) -> None:
        if isinstance(position.quantity, bool) or not isinstance(position.quantity, int) or position.quantity < 0:
            raise ValueError(f"{position.symbol}: only nonnegative whole-share positions are supported.")
        if not isfinite(position.average_cost) or position.average_cost < 0 or (position.quantity and position.average_cost <= 0):
            raise ValueError(f"{position.symbol}: invalid average position cost.")
