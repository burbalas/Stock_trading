from __future__ import annotations

from .config import BotConfig
from .models import Order, Signal, Side
from .portfolio import PortfolioManager


class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def build_order(self, signal: Signal, price: float, broker, equity: float) -> Order | None:
        if signal.side is Side.HOLD or signal.confidence < self.config.min_confidence:
            return None

        position = broker.positions.get(signal.symbol)
        current_quantity = position.quantity if position else 0
        average_cost = position.average_cost if position else 0.0

        if signal.side is Side.SELL:
            if current_quantity <= 0:
                return None
            return Order(signal.symbol, Side.SELL, current_quantity, price, signal.reason)

        max_position_value = equity * self.config.max_position_pct
        current_value = current_quantity * price
        remaining_position_room = max(0.0, max_position_value - current_value)
        max_order_value = min(equity * self.config.max_order_pct, broker.cash, remaining_position_room)
        quantity = int(max_order_value // price)

        if quantity <= 0:
            return None

        if average_cost and price <= average_cost * (1 - self.config.stop_loss_pct):
            return None

        return Order(signal.symbol, Side.BUY, quantity, price, signal.reason)

    def stop_loss_order(self, symbol: str, price: float, broker) -> Order | None:
        position = broker.positions.get(symbol)
        if not position or position.quantity <= 0 or position.average_cost <= 0:
            return None
        if price <= position.average_cost * (1 - self.config.stop_loss_pct):
            return Order(symbol, Side.SELL, position.quantity, price, "stop loss")
        return None


class PortfolioRiskManager(PortfolioManager):
    """Compatibility alias for the scalable portfolio-level risk layer."""
