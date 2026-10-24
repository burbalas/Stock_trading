from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import math

from .advisor import Advisor
from .config import BotConfig
from .journal import TradeJournal
from .models import Fill, Order, Signal, Side
from .portfolio import PortfolioManager
from .strategies import Strategy, generate_strategy_signals
from .time_utils import MARKET_TZ


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
    execution_mode: str = "alpaca-paper"


class _BuyControlRejected(RuntimeError):
    """An operator control changed after an entry was planned."""


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
        self.execution_mode = getattr(broker, "execution_mode", "alpaca-paper")
        self.portfolio = PortfolioManager(config)
        self.journal = journal or TradeJournal()
        self.journal.bind_account(getattr(self.broker, "account_id", None))

    def run_once(self, submit_orders: bool = False) -> LiveRunResult:
        if hasattr(self.broker, "refresh"):
            self.broker.refresh()
        market_data = self.data_provider.load(self.config.symbols)
        if hasattr(self.broker, "latest_completed_session"):
            expected_date = self.broker.latest_completed_session()
            market_data = {symbol: [row for row in rows if row.date <= expected_date]
                           for symbol, rows in market_data.items()}
            invalid = [symbol for symbol in self.config.symbols
                       if not market_data.get(symbol) or market_data[symbol][-1].date != expected_date]
            if invalid:
                raise RuntimeError(f"Missing or stale completed daily bars for {', '.join(invalid)}; expected {expected_date}.")
        latest_prices = {
            symbol: rows[-1].close
            for symbol, rows in market_data.items()
            if rows
        }
        latest_date = max((rows[-1].date for rows in market_data.values() if rows), default=None)
        snapshot = self.broker.snapshot(latest_prices, latest_date)
        _, account_drawdown, drawdown_halted = self.journal.update_drawdown_state(
            snapshot.equity, self.config.max_drawdown_stop_pct,
        )
        if submit_orders and drawdown_halted:
            self._cancel_pending_buys_for_halt()
            snapshot = self.broker.snapshot(latest_prices, latest_date)
        self._validate_managed_positions()
        order_date = self._order_date(latest_date)
        signals: list[Signal] = []

        held_symbols = {
            symbol
            for symbol, position in self.broker.positions.items()
            if position.quantity > 0
        }
        # Risk exits must cover the whole account, including holdings removed
        # from the signal universe, and use current broker marks.
        latest_prices.update(self._latest_position_prices(held_symbols))
        high_prices = {
            symbol: max(latest_prices.get(symbol, 0.0), self.broker.positions[symbol].average_cost)
            for symbol in held_symbols
        }
        position_highs = self.journal.update_position_highs(high_prices, held_symbols)
        risk_drawdown = max(account_drawdown, self.config.max_drawdown_stop_pct) if drawdown_halted else account_drawdown
        stop_orders = self.portfolio.account_drawdown_orders(latest_prices, self.broker, risk_drawdown)
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

        planning_prices = dict(latest_prices)
        entry_quote_rejections: dict[str, str] = {}
        if (self.config.allow_new_buys and not drawdown_halted
                and hasattr(self.data_provider, "latest_entry_prices")):
            exit_symbols = {signal.symbol for signal in signals if signal.side is Side.SELL}
            entry_symbols = sorted({signal.symbol for signal in signals
                                    if signal.side is Side.BUY and signal.confidence >= self.config.min_confidence
                                    and signal.symbol not in exit_symbols
                                    and (signal.symbol not in held_symbols or self.config.allow_position_adds)})
            for symbol in entry_symbols:
                try:
                    entry_prices = self.data_provider.latest_entry_prices([symbol])
                    price = entry_prices.get(symbol) if isinstance(entry_prices, dict) else None
                    if not self.portfolio._valid_price(price):
                        raise RuntimeError("fresh entry quote is missing or invalid; refusing stale-price sizing")
                    planning_prices[symbol] = price
                except (RuntimeError, ValueError, TypeError) as exc:
                    # A single illiquid or stale symbol must not prevent other
                    # entries or account-protecting exits from being evaluated.
                    entry_quote_rejections[symbol] = f"fresh entry quote rejected: {exc}"

        plan = self.portfolio.build_order_plan(
            [signal for signal in signals
             if signal.side is not Side.BUY or signal.symbol not in entry_quote_rejections],
            planning_prices,
            self.broker,
            self._sizing_equity(snapshot.equity),
            as_of_date=order_date,
            recent_exit_dates=self.journal.recent_exit_dates(),
        )
        planned_orders = [self._with_client_order_id(order, order_date) for order in [*stop_orders, *plan.orders]]
        drawdown_rejections: dict[str, str] = {}
        if drawdown_halted:
            allowed_orders = []
            for order in planned_orders:
                if order.side is Side.SELL:
                    allowed_orders.append(order)
                else:
                    drawdown_rejections[order.symbol] = "account drawdown stop is latched; review required before resuming buys"
            planned_orders = allowed_orders

        existing_client_ids = self._existing_client_order_ids()
        pending_buys = self._pending_buy_orders()
        executable_orders: list[Order] = []
        duplicate_rejections: dict[str, str] = {}
        daily_buy_count = self.journal.submitted_count_for_date(order_date, Side.BUY.value)
        for order in planned_orders:
            key = TradeJournal.order_key(order, order_date)
            if self.journal.has_submitted(key) or (order.client_order_id and order.client_order_id in existing_client_ids):
                retry = self._prepare_risk_exit_retry(order, order_date, submit_orders)
                if retry is None:
                    duplicate_rejections[order.symbol] = "duplicate order already submitted"
                    continue
                order = retry
            if order.side is Side.BUY and pending_buys:
                duplicate_rejections[order.symbol] = "unresolved buy orders exist; waiting for broker reconciliation"
                continue
            if order.side is Side.BUY and daily_buy_count >= self.config.max_daily_orders:
                duplicate_rejections[order.symbol] = "max daily buy orders reached"
                continue
            executable_orders.append(order)
            if order.side is Side.BUY:
                daily_buy_count += 1

        order_by_symbol = {order.symbol: order for order in executable_orders}
        fills_by_symbol: dict[str, Fill] = {}
        submission_rejections: dict[str, str] = {}

        if submit_orders:
            for order in executable_orders:
                if order.side is Side.BUY:
                    checked_order, rejection = self._revalidate_buy(order, order_date)
                    if checked_order is None:
                        order_by_symbol.pop(order.symbol, None)
                        submission_rejections[order.symbol] = rejection or "buy failed execution revalidation"
                        continue
                    order = checked_order
                    order_by_symbol[order.symbol] = order
                try:
                    fill = self._submit_order(order, order_date, self.execution_mode)
                except _BuyControlRejected as exc:
                    order_by_symbol.pop(order.symbol, None)
                    submission_rejections[order.symbol] = str(exc)
                    continue
                if fill:
                    fills_by_symbol[fill.symbol] = fill
                if order.side is Side.BUY and hasattr(self.broker, "after_buy_submission"):
                    self.broker.after_buy_submission()

        rejected_by_symbol: dict[str, str] = {}
        for rejection in plan.rejected:
            symbol, _, reason = rejection.partition(": ")
            rejected_by_symbol[symbol] = reason or rejection
        rejected_by_symbol.update(entry_quote_rejections)
        rejected_by_symbol.update(drawdown_rejections)
        rejected_by_symbol.update(duplicate_rejections)
        rejected_by_symbol.update(submission_rejections)
        if submit_orders:
            latest_prices.update(self._latest_position_prices(
                {symbol for symbol, position in self.broker.positions.items() if position.quantity > 0},
            ))
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
            self.journal.record_decision(decision.signal, decision.order, final_snapshot.equity, self.execution_mode, None)

        decision_symbols = {decision.signal.symbol for decision in decisions}
        for signal in signals:
            if signal.symbol in decision_symbols:
                continue
            order = order_by_symbol.get(signal.symbol)
            rejected_reason = rejected_by_symbol.get(signal.symbol)
            self.journal.record_decision(signal, order, final_snapshot.equity, self.execution_mode, rejected_reason)
            decisions.append(
                LiveDecision(
                    signal=signal,
                    order=order,
                    fill=fills_by_symbol.get(signal.symbol),
                    rejected_reason=rejected_reason,
                )
            )

        return LiveRunResult(decisions=decisions, equity=final_snapshot.equity, execution_mode=self.execution_mode)

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
        _, account_drawdown, drawdown_halted = self.journal.update_drawdown_state(
            snapshot.equity, self.config.max_drawdown_stop_pct,
        )
        if submit_orders and drawdown_halted:
            self._cancel_pending_buys_for_halt()
            held_symbols = {symbol for symbol, position in self.broker.positions.items() if position.quantity > 0}
            latest_prices = self._latest_position_prices(held_symbols)
        self._validate_managed_positions()
        order_date = self._order_date(None)

        high_prices = {
            symbol: max(latest_prices.get(symbol, 0.0), self.broker.positions[symbol].average_cost)
            for symbol in held_symbols
        }
        position_highs = self.journal.update_position_highs(high_prices, held_symbols)
        risk_drawdown = max(account_drawdown, self.config.max_drawdown_stop_pct) if drawdown_halted else account_drawdown
        raw_stop_orders = self.portfolio.account_drawdown_orders(latest_prices, self.broker, risk_drawdown)
        if not raw_stop_orders:
            raw_stop_orders = self.portfolio.stop_loss_orders(latest_prices, self.broker, position_highs)
        stop_orders = [
            self._with_client_order_id(order, order_date)
            for order in raw_stop_orders
        ]
        existing_client_ids = self._existing_client_order_ids()
        executable_orders: list[Order] = []
        duplicate_rejections: dict[str, str] = {}
        for order in stop_orders:
            key = TradeJournal.order_key(order, order_date)
            if self.journal.has_submitted(key) or (order.client_order_id and order.client_order_id in existing_client_ids):
                retry = self._prepare_risk_exit_retry(order, order_date, submit_orders)
                if retry is None:
                    duplicate_rejections[order.symbol] = "duplicate order already submitted"
                    continue
                order = retry
            executable_orders.append(order)

        fills_by_symbol: dict[str, Fill] = {}
        if submit_orders:
            for order in executable_orders:
                fill = self._submit_order(order, order_date, self.execution_mode + "-risk")
                if fill:
                    fills_by_symbol[fill.symbol] = fill

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
            self.journal.record_decision(signal, order, final_snapshot.equity, self.execution_mode + "-risk", rejected_reason)

        return LiveRunResult(decisions=decisions, equity=final_snapshot.equity, execution_mode=self.execution_mode)

    def _prepare_risk_exit_retry(self, order: Order, order_date, submit_orders: bool) -> Order | None:
        if (not submit_orders or not self._is_risk_exit(order)
                or not hasattr(self.broker, "order_by_client_id") or not hasattr(self.broker, "refresh")):
            return None
        previous_id = self.journal.latest_risk_exit_client_id(order, order_date)
        if previous_id is None:
            return None
        # A direct lookup, rather than a cached journal status, must establish
        # that the previous attempt can no longer execute.
        remote = self.broker.order_by_client_id(previous_id)
        if remote is None:
            return None
        self.broker.refresh()
        self._validate_managed_positions()
        position = self.broker.positions.get(order.symbol)
        if not position or position.quantity <= 0:
            return None
        price = self._latest_position_prices({order.symbol}).get(order.symbol)
        if price is None:
            return None
        refreshed = replace(order, quantity=min(order.quantity, position.quantity), price=price)
        return self.journal.prepare_risk_exit_retry(refreshed, order_date, remote)

    def _submit_order(self, order: Order, order_date, mode: str) -> Fill | None:
        if order.side is Side.SELL:
            self._cancel_open_sell_orders(order.symbol)
            self.journal.clear_protective_order(order.symbol)
            position = self.broker.positions.get(order.symbol)
            if not position or position.quantity <= 0:
                return None
            order = replace(order, quantity=min(order.quantity, position.quantity))
        if order.side is Side.BUY:
            rejection = self._buy_control_rejection()
            if rejection:
                raise _BuyControlRejected(rejection)
        if not self.journal.reserve_order(order, order_date, self.config.max_daily_orders):
            return None
        fill = self.broker.submit(order, order_date)
        remote = getattr(self.broker, "last_submitted_order", None)
        if remote is not None:
            fill = self.journal.record_broker_order(remote, mode=mode)
            status = getattr(remote, "status", "")
            self.journal.update_order_status(order.client_order_id, str(getattr(status, "value", status)))
        elif fill:
            self.journal.record_fill(fill, mode=mode)
            self.journal.update_order_status(order.client_order_id, "filled")
        if fill and fill.side is Side.SELL and fill.quantity == order.quantity:
            self.journal.clear_protective_order(fill.symbol)
        return fill

    def _buy_control_rejection(self) -> str | None:
        # TradeJournal resolves migration redirects to the canonical runtime.
        # Read controls again for each entry; the configuration was captured
        # before potentially slow broker and quote requests.
        if (self.journal.directory / "STOP").exists():
            return "execution revalidation: operator stop requested"
        if (self.journal.directory / "STOP_BUYS").exists():
            return "execution revalidation: new buys paused by operator"
        return None

    def _sizing_equity(self, equity):
        return self.broker.sizing_equity(equity) if hasattr(self.broker, "sizing_equity") else equity

    def _revalidate_buy(self, order: Order, order_date) -> tuple[Order | None, str | None]:
        """Recheck an entry against current account state before reserving its ID.

        Planning can precede several broker round trips. In particular, an
        earlier order can fill, remain pending, or consume cash while the next
        entry waits. A fresh plan must therefore pass for every submitted buy;
        its quantity may decrease, but can never exceed the original plan.
        """
        try:
            rejection = self._buy_control_rejection()
            if rejection:
                return None, rejection
            if hasattr(self.broker, "refresh"):
                self.broker.refresh()
            self._validate_managed_positions()
            held_symbols = {symbol for symbol, position in self.broker.positions.items()
                            if position.quantity > 0}
            prices = self._latest_position_prices(held_symbols)
            if hasattr(self.data_provider, "latest_entry_prices"):
                entry_prices = self.data_provider.latest_entry_prices([order.symbol])
                if order.symbol not in entry_prices:
                    return None, "execution revalidation: fresh entry quote is missing"
                prices[order.symbol] = entry_prices[order.symbol]
            elif hasattr(self.broker, "refresh"):
                # Static in-memory simulations can execute their supplied
                # prices. A connected broker must have a fresh-quote provider.
                return None, "execution revalidation: fresh entry quotes are unavailable"
            else:
                prices[order.symbol] = order.price

            snapshot = self.broker.snapshot(prices, order_date)
            _, _, halted = self.journal.update_drawdown_state(
                snapshot.equity, self.config.max_drawdown_stop_pct,
            )
            if halted:
                self._cancel_pending_buys_for_halt()
                return None, "execution revalidation: account drawdown stop is latched"
            if self._pending_buy_orders():
                return None, "execution revalidation: unresolved buy orders exist"
            if self.journal.submitted_count_for_date(order_date, Side.BUY.value) >= self.config.max_daily_orders:
                return None, "execution revalidation: max daily buy orders reached"

            fresh_plan = self.portfolio.build_order_plan(
                [Signal(order.symbol, Side.BUY, 1.0, order.reason)], prices, self.broker,
                self._sizing_equity(snapshot.equity), as_of_date=order_date,
                recent_exit_dates=self.journal.recent_exit_dates(),
            )
            if not fresh_plan.orders:
                reason = fresh_plan.rejected[0] if fresh_plan.rejected else "entry is no longer eligible"
                return None, "execution revalidation: " + reason
            position = self.broker.positions.get(order.symbol)
            quantity = min(
                order.quantity,
                fresh_plan.orders[0].quantity,
                self.portfolio.max_buy_quantity(
                    (self.broker.maximum_entry_price(prices[order.symbol])
                     if hasattr(self.broker, "maximum_entry_price") else prices[order.symbol]),
                    self._sizing_equity(snapshot.equity), self.broker.cash,
                    position.quantity if position else 0,
                ),
            )
            if hasattr(self.broker, "constrain_buy_quantity"):
                quantity = self.broker.constrain_buy_quantity(order.symbol, quantity, prices[order.symbol])
            if quantity <= 0 or quantity * prices[order.symbol] < self.config.min_trade_value:
                return None, "execution revalidation: quantity rounded to zero or order value below minimum"
            rejection = self._buy_control_rejection()
            if rejection:
                return None, rejection
            return replace(order, quantity=quantity, price=prices[order.symbol]), None
        except (RuntimeError, ValueError, TypeError) as exc:
            # No intent has been reserved and no order has been sent. Keep a
            # visible decision while allowing later account protection checks.
            return None, f"execution revalidation failed: {exc}"

    def _cancel_pending_buys_for_halt(self) -> None:
        if not hasattr(self.broker, "cancel_open_buy_orders"):
            if self._pending_buy_orders():
                raise RuntimeError("Drawdown halt cannot confirm pending buy cancellations with this broker.")
            return
        from .alpaca_integration import TERMINAL_ORDER_STATUSES, _order_status
        from .reconcile import sync_broker_orders

        intents = {client_id for client_id, details in self.journal._read_state().get("order_intents", {}).items()
                   if details.get("side") == "buy" and details.get("status") not in TERMINAL_ORDER_STATUSES}
        confirmed = self.broker.cancel_open_buy_orders(intents)
        # The existing synchronizer checks replacement histories before recording
        # fills. Direct confirmations cover closed-list propagation delays, with
        # the same cumulative-fill cursor making replay idempotent.
        sync_broker_orders(self.journal, self.broker)
        for remote in confirmed:
            self.journal.record_broker_order(remote, mode=self.execution_mode + "-halt")
            self.journal.update_order_status(str(remote.client_order_id), _order_status(remote))
        self.broker.refresh()

    def _validate_managed_positions(self) -> None:
        unsupported = [symbol for symbol, position in self.broker.positions.items()
                       if position.quantity < 0 or not float(position.quantity).is_integer()]
        if unsupported:
            raise RuntimeError("Whole-share long-only trading cannot manage these positions: "
                               + ", ".join(sorted(unsupported))
                               + ". Synchronize fills and wait for partial orders to complete, or reconcile holdings before trading.")

    def _pending_buy_orders(self) -> bool:
        if hasattr(self.broker, "open_orders"):
            for order in self.broker.open_orders():
                side = getattr(order, "side", "")
                if str(getattr(side, "value", side)).lower() == "buy":
                    return True
        terminal = {"filled", "canceled", "expired", "rejected", "replaced"}
        return any(details.get("side") == "buy" and details.get("status") not in terminal
                   for details in self.journal._read_state().get("order_intents", {}).values())

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
            return self.broker.open_client_order_ids()
        return set()

    def _open_sell_order_symbols(self) -> set[str]:
        if not hasattr(self.broker, "open_orders"):
            return set()
        symbols: set[str] = set()
        for order in self.broker.open_orders():
            side = getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))
            status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))).lower()
            if status in {"replaced", "canceled", "expired", "filled", "rejected"}:
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
        return fallback_date or datetime.now(MARKET_TZ).date()

    def _latest_position_prices(self, held_symbols: set[str]) -> dict[str, float]:
        broker_prices = getattr(self.broker, "position_prices", {})
        latest_prices = {
            symbol: float(price)
            for symbol, price in broker_prices.items()
            if symbol in held_symbols and math.isfinite(float(price)) and float(price) > 0
        }
        missing_symbols = [symbol for symbol in held_symbols if symbol not in latest_prices]
        if missing_symbols and hasattr(self.broker, "position_prices"):
            raise RuntimeError(f"Missing valid current broker marks for {', '.join(sorted(missing_symbols))}.")
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
