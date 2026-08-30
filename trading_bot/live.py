from __future__ import annotations

from dataclasses import dataclass

from .advisor import Advisor
from .config import BotConfig
from .journal import TradeJournal
from .models import Fill, Order, Signal, Side
from .portfolio import PortfolioManager
from .strategies import Strategy, generate_strategy_signals


@dataclass(frozen=True)
class LiveDecision:
    signal: Signal
    order: Order | None
    fill: Fill | None
    rejected_reason: str | None = None


@dataclass(frozen=True)
class LiveRunResult:
    decisions: list[LiveDecision]
    equity: float


class LiveTradingEngine:
    def __init__(
        self,
        config: BotConfig,
        data_provider,
        strategy: Strategy,
        advisor: Advisor,
        broker,
        journal: TradeJournal | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.data_provider = data_provider
        self.strategy = strategy
        self.advisor = advisor
        self.broker = broker
        self.portfolio = PortfolioManager(config)
        self.journal = journal or TradeJournal()
        self.journal.bind_account(getattr(self.broker, "account_id", None))

    def run_once(self, submit_orders: bool = False) -> LiveRunResult:
        market_data = self.data_provider.load(self.config.symbols)
        latest_prices = {
            symbol: rows[-1].close
            for symbol, rows in market_data.items()
            if rows
        }
        latest_date = max((rows[-1].date for rows in market_data.values() if rows), default=None)
        snapshot = self.broker.snapshot(latest_prices, latest_date)
        _, account_drawdown = self.journal.update_peak_equity(snapshot.equity)
        order_date = self._order_date(latest_date)
        signals: list[Signal] = []

        held_symbols = {
            symbol
            for symbol, position in self.broker.positions.items()
            if position.quantity > 0
        }
        high_prices = {
            symbol: max(latest_prices.get(symbol, 0.0), self.broker.positions[symbol].average_cost)
            for symbol in held_symbols
        }
        position_highs = self.journal.update_position_highs(high_prices, held_symbols)
        stop_orders = self.portfolio.account_drawdown_orders(latest_prices, self.broker, account_drawdown)
        if not stop_orders:
            stop_orders = self.portfolio.stop_loss_orders(latest_prices, self.broker, position_highs)
        stopped_symbols = {order.symbol for order in stop_orders}
        signal_histories = {
            symbol.upper(): market_data.get(symbol.upper(), [])
            for symbol in self.config.symbols
            if market_data.get(symbol.upper()) and symbol.upper() not in stopped_symbols
        }
        for signal in generate_strategy_signals(self.strategy, signal_histories):
            signals.append(self.advisor.review(signal, signal_histories[signal.symbol]))

        plan = self.portfolio.build_order_plan(
            signals,
            latest_prices,
            self.broker,
            snapshot.equity,
            as_of_date=order_date,
            recent_exit_dates=self.journal.recent_exit_dates(),
        )
        planned_orders = [self._with_client_order_id(order, order_date) for order in [*stop_orders, *plan.orders]]
        drawdown_rejections: dict[str, str] = {}
        if account_drawdown >= self.config.max_drawdown_stop_pct:
            allowed_orders = []
            for order in planned_orders:
                if order.side is Side.SELL:
                    allowed_orders.append(order)
                else:
                    drawdown_rejections[order.symbol] = "account drawdown stop is active"
            planned_orders = allowed_orders

        existing_client_ids = self._existing_client_order_ids()
        open_sell_symbols = self._open_sell_order_symbols()
        executable_orders: list[Order] = []
        duplicate_rejections: dict[str, str] = {}
        daily_buy_count = self.journal.submitted_count_for_date(order_date, Side.BUY.value)
        for order in planned_orders:
            key = TradeJournal.order_key(order, order_date)
            if self.journal.has_submitted(key) or (order.client_order_id and order.client_order_id in existing_client_ids):
                duplicate_rejections[order.symbol] = "duplicate order already submitted"
                continue
            if self._is_protective_exit(order) and order.symbol in open_sell_symbols:
                duplicate_rejections[order.symbol] = "open sell order already exists"
                continue
            if order.side is Side.BUY and daily_buy_count >= self.config.max_daily_orders:
                duplicate_rejections[order.symbol] = "max daily buy orders reached"
                continue
            executable_orders.append(order)
            if order.side is Side.BUY:
                daily_buy_count += 1

        order_by_symbol = {order.symbol: order for order in executable_orders}
        fills_by_symbol: dict[str, Fill] = {}

        if submit_orders:
            for order in executable_orders:
                if order.side is Side.SELL and not self._is_protective_exit(order):
                    self._cancel_open_sell_orders(order.symbol)
                fill = self.broker.submit(order, order_date)
                if fill:
                    fills_by_symbol[fill.symbol] = fill
                    self.journal.mark_submitted(TradeJournal.order_key(order, order_date))
                    self.journal.record_fill(fill, mode="alpaca-paper")
                    if fill.side is Side.SELL:
                        self.journal.clear_protective_order(fill.symbol)

        rejected_by_symbol: dict[str, str] = {}
        for rejection in plan.rejected:
            symbol, _, reason = rejection.partition(": ")
            rejected_by_symbol[symbol] = reason or rejection
        rejected_by_symbol.update(drawdown_rejections)
        rejected_by_symbol.update(duplicate_rejections)
        final_snapshot = self.broker.snapshot(latest_prices, latest_date)

        decisions: list[LiveDecision] = [
            LiveDecision(
                signal=Signal(order.symbol, Side.SELL, 1.0, order.reason) if self._is_risk_exit(order) else next(
                    (signal for signal in signals if signal.symbol == order.symbol),
                    Signal(order.symbol, order.side, 1.0, order.reason),
                ),
                order=order,
                fill=fills_by_symbol.get(order.symbol),
            )
            for order in executable_orders
            if self._is_risk_exit(order)
        ]
        for decision in decisions:
            self.journal.record_decision(decision.signal, decision.order, final_snapshot.equity, "alpaca-paper", None)

        decision_symbols = {decision.signal.symbol for decision in decisions}
        for signal in signals:
            if signal.symbol in decision_symbols:
                continue
            order = order_by_symbol.get(signal.symbol)
            rejected_reason = rejected_by_symbol.get(signal.symbol)
            self.journal.record_decision(signal, order, final_snapshot.equity, "alpaca-paper", rejected_reason)
            decisions.append(
                LiveDecision(
                    signal=signal,
                    order=order,
                    fill=fills_by_symbol.get(signal.symbol),
                    rejected_reason=rejected_reason,
                )
            )

        return LiveRunResult(decisions=decisions, equity=final_snapshot.equity)

    def run_risk_once(self, submit_orders: bool = False) -> LiveRunResult:
        if hasattr(self.broker, "refresh"):
            self.broker.refresh()
        held_symbols = {
            symbol
            for symbol, position in self.broker.positions.items()
            if position.quantity > 0
        }
        latest_prices = self._latest_position_prices(held_symbols)
        snapshot = self.broker.snapshot(latest_prices, self._order_date(None))
        _, account_drawdown = self.journal.update_peak_equity(snapshot.equity)
        order_date = self._order_date(None)

        high_prices = {
            symbol: max(latest_prices.get(symbol, 0.0), self.broker.positions[symbol].average_cost)
            for symbol in held_symbols
        }
        position_highs = self.journal.update_position_highs(high_prices, held_symbols)
        raw_stop_orders = self.portfolio.account_drawdown_orders(latest_prices, self.broker, account_drawdown)
        if not raw_stop_orders:
            raw_stop_orders = self.portfolio.stop_loss_orders(latest_prices, self.broker, position_highs)
        stop_orders = [
            self._with_client_order_id(order, order_date)
            for order in raw_stop_orders
        ]
        existing_client_ids = self._existing_client_order_ids()
        open_sell_symbols = self._open_sell_order_symbols()
        executable_orders: list[Order] = []
        duplicate_rejections: dict[str, str] = {}
        for order in stop_orders:
            key = TradeJournal.order_key(order, order_date)
            if self.journal.has_submitted(key) or (order.client_order_id and order.client_order_id in existing_client_ids):
                duplicate_rejections[order.symbol] = "duplicate order already submitted"
                continue
            if self._is_protective_exit(order) and order.symbol in open_sell_symbols:
                duplicate_rejections[order.symbol] = "open sell order already exists"
                continue
            executable_orders.append(order)

        fills_by_symbol: dict[str, Fill] = {}
        if submit_orders:
            for order in executable_orders:
                if order.side is Side.SELL and not self._is_protective_exit(order):
                    self._cancel_open_sell_orders(order.symbol)
                fill = self.broker.submit(order, order_date)
                if fill:
                    fills_by_symbol[fill.symbol] = fill
                    self.journal.mark_submitted(TradeJournal.order_key(order, order_date))
                    self.journal.record_fill(fill, mode="alpaca-paper-risk")
                    if fill.side is Side.SELL:
                        self.journal.clear_protective_order(fill.symbol)

        if hasattr(self.broker, "refresh"):
            self.broker.refresh()
        final_snapshot = self.broker.snapshot(latest_prices, order_date)
        order_by_symbol = {order.symbol: order for order in executable_orders}
        decisions: list[LiveDecision] = []
        for symbol in sorted(held_symbols):
            order = order_by_symbol.get(symbol)
            signal = Signal(
                symbol=symbol,
                side=Side.SELL if order else Side.HOLD,
                confidence=1.0 if order else 0.0,
                reason=order.reason if order else "risk check: no stop triggered",
            )
            rejected_reason = duplicate_rejections.get(symbol)
            decision = LiveDecision(
                signal=signal,
                order=order,
                fill=fills_by_symbol.get(symbol),
                rejected_reason=rejected_reason,
            )
            decisions.append(decision)
            self.journal.record_decision(signal, order, final_snapshot.equity, "alpaca-paper-risk", rejected_reason)

        return LiveRunResult(decisions=decisions, equity=final_snapshot.equity)

    def _with_client_order_id(self, order: Order, trade_date) -> Order:
        return Order(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            reason=order.reason,
            client_order_id=TradeJournal.client_order_id(order, trade_date),
        )

    def _existing_client_order_ids(self) -> set[str]:
        if hasattr(self.broker, "open_client_order_ids"):
            return self.broker.open_client_order_ids(self.config.symbols)
        return set()

    def _open_sell_order_symbols(self) -> set[str]:
        if not hasattr(self.broker, "open_orders"):
            return set()
        symbols: set[str] = set()
        for order in self.broker.open_orders(self.config.symbols):
            side = getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))
            status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))).lower()
            if status in {"pending_replace", "pending_cancel", "replaced", "canceled", "expired"}:
                continue
            if str(side).lower() == Side.SELL.value:
                symbols.add(str(order.symbol).upper())
        return symbols

    def _cancel_open_sell_orders(self, symbol: str) -> None:
        if hasattr(self.broker, "cancel_open_sell_orders"):
            self.broker.cancel_open_sell_orders(symbol)

    def _is_protective_exit(self, order: Order) -> bool:
        return order.side is Side.SELL and order.reason in {"stop loss", "trailing stop"}

    def _is_risk_exit(self, order: Order) -> bool:
        return order.side is Side.SELL and order.reason in {
            "stop loss",
            "trailing stop",
            "account drawdown stop",
        }

    def _order_date(self, fallback_date):
        if hasattr(self.broker, "trading_date"):
            return self.broker.trading_date()
        return fallback_date

    def _latest_position_prices(self, held_symbols: set[str]) -> dict[str, float]:
        broker_prices = getattr(self.broker, "position_prices", {})
        latest_prices = {
            symbol: float(price)
            for symbol, price in broker_prices.items()
            if symbol in held_symbols and float(price) > 0
        }
        missing_symbols = [symbol for symbol in held_symbols if symbol not in latest_prices]
        if missing_symbols:
            market_data = self.data_provider.load(missing_symbols)
            latest_prices.update(
                {
                    symbol: rows[-1].close
                    for symbol, rows in market_data.items()
                    if rows
                }
            )
        return latest_prices
