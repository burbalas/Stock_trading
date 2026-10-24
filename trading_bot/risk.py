from __future__ import annotations

from .config import BotConfig
from .models import Order, Signal
from .portfolio import PortfolioManager


class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.portfolio = PortfolioManager(config)

    def build_order(self, signal: Signal, price: float, broker, equity: float) -> Order | None:
        prices = dict(getattr(broker, "position_prices", {}))
        prices[signal.symbol] = price
        plan = self.portfolio.build_order_plan([signal], prices, broker, equity)
        return plan.orders[0] if plan.orders else None

    def stop_loss_order(self, symbol: str, price: float, broker) -> Order | None:
        orders = self.portfolio.stop_loss_orders({symbol: price}, broker)
        return orders[0] if orders else None


class PortfolioRiskManager(PortfolioManager):
    """Compatibility alias for the scalable portfolio-level risk layer."""
