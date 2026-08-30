from __future__ import annotations

from .models import Fill, Order, PortfolioSnapshot, Position, Side


class PaperBroker:
    def __init__(self, starting_cash: float, slippage_bps: float = 0.0, commission_per_trade: float = 0.0) -> None:
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.slippage_bps = slippage_bps
        self.commission_per_trade = commission_per_trade

    def submit(self, order: Order, trade_date) -> Fill | None:
        if order.quantity <= 0:
            return None

        execution_price = self._execution_price(order)
        value = order.quantity * execution_price
        position = self.positions.setdefault(order.symbol, Position(order.symbol))

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
            position.quantity -= sell_quantity
            if position.quantity == 0:
                position.average_cost = 0.0
            self.cash += max(0.0, value - self.commission_per_trade)
            order = Order(order.symbol, order.side, sell_quantity, execution_price, order.reason, order.client_order_id)
        else:
            return None

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
            return order.price * (1 + slip)
        if order.side is Side.SELL:
            return order.price * (1 - slip)
        return order.price

    def snapshot(self, prices: dict[str, float], current_date) -> PortfolioSnapshot:
        positions_value = sum(
            position.market_value(prices.get(symbol, 0.0))
            for symbol, position in self.positions.items()
        )
        return PortfolioSnapshot(
            date=current_date,
            cash=self.cash,
            positions_value=positions_value,
            equity=self.cash + positions_value,
        )
