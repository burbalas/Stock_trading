from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from datetime import date

from .advisor import Advisor
from .broker import PaperBroker
from .config import BotConfig
from .data import MarketDataProvider, validate_market_data
from .models import Fill, Order, PortfolioSnapshot, Side
from .portfolio import PortfolioManager
from .strategies import Strategy, generate_strategy_signals


@dataclass(frozen=True)
class RunResult:
    snapshots: list[PortfolioSnapshot]
    fills: list[Fill]

    @property
    def final_equity(self) -> float:
        return self.snapshots[-1].equity if self.snapshots else 0.0

    @property
    def return_pct(self) -> float:
        if not self.snapshots:
            return 0.0
        start = self.snapshots[0].equity
        return ((self.final_equity - start) / start) * 100


class TradingEngine:
    def __init__(
        self,
        config: BotConfig,
        data_provider: MarketDataProvider,
        strategy: Strategy,
        advisor: Advisor,
        trade_start_date: date | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.data_provider = data_provider
        self.strategy = strategy
        self.advisor = advisor
        self.trade_start_date = trade_start_date
        self.portfolio = PortfolioManager(config)

    def run(self) -> RunResult:
        market_data = validate_market_data(self.data_provider.load(self.config.symbols), self.config.symbols)
        broker = PaperBroker(
            self.config.starting_cash,
            slippage_bps=self.config.slippage_bps,
            commission_per_trade=self.config.commission_per_trade,
        )
        histories = {symbol.upper(): [] for symbol in self.config.symbols}
        snapshots: list[PortfolioSnapshot] = []
        candles_by_date = defaultdict(list)
        for rows in market_data.values():
            for candle in rows:
                candles_by_date[candle.date].append(candle)
        latest_prices: dict[str, float] = {}
        peak_equity = self.config.starting_cash
        pending_orders: list[Order] = []
        position_highs: dict[str, float] = {}
        drawdown_halted = False

        for current_date in sorted(candles_by_date):
            todays_candles = candles_by_date[current_date]

            open_prices = {candle.symbol: candle.open for candle in todays_candles}
            for order in pending_orders:
                price = open_prices.get(order.symbol)
                if price is None:
                    # Simulated DAY orders expire instead of accumulating across
                    # missing sessions and filling on an arbitrarily later bar.
                    continue
                position = broker.positions.get(order.symbol)
                held_quantity = position.quantity if position else 0
                quantity = order.quantity
                if order.side is Side.BUY:
                    open_equity = broker.snapshot({**latest_prices, **open_prices}, current_date).equity
                    quantity = min(quantity, self.portfolio.max_buy_quantity(
                        price, open_equity, broker.cash, held_quantity
                    ))
                    if quantity <= 0:
                        continue
                fill = broker.submit(
                    Order(
                        symbol=order.symbol,
                        side=order.side,
                        quantity=quantity,
                        price=price,
                        reason=order.reason,
                        client_order_id=order.client_order_id,
                    ),
                    current_date,
                )
                if fill is not None and fill.side is Side.BUY and held_quantity == 0:
                    position_highs[order.symbol] = fill.price
            pending_orders = []
            position_highs = {
                symbol: high for symbol, high in position_highs.items()
                if broker.positions.get(symbol) and broker.positions[symbol].quantity > 0
            }

            for candle in todays_candles:
                histories[candle.symbol].append(candle)
                latest_prices[candle.symbol] = candle.close

            trading_enabled = self.trade_start_date is None or current_date >= self.trade_start_date
            if not trading_enabled:
                # Warm-up builds indicator history, not the measured return series.
                continue

            candles_by_symbol = {candle.symbol: candle for candle in todays_candles}
            stop_orders = self.portfolio.bar_stop_orders(candles_by_symbol, broker, position_highs)
            stopped_symbols = {order.symbol for order in stop_orders}
            for order in stop_orders:
                broker.submit(order, current_date)
            held_symbols = {
                symbol
                for symbol, position in broker.positions.items()
                if position.quantity > 0
            }
            position_highs = {
                symbol: max(position_highs.get(symbol, broker.positions[symbol].average_cost),
                            candles_by_symbol[symbol].high if symbol in candles_by_symbol else 0.0)
                for symbol in held_symbols
            }

            snapshot = broker.snapshot(latest_prices, current_date)
            peak_equity = max(peak_equity, snapshot.equity)
            drawdown = (peak_equity - snapshot.equity) / peak_equity if peak_equity else 0.0
            drawdown_halted = drawdown_halted or drawdown >= self.config.max_drawdown_stop_pct
            if drawdown_halted:
                # The close is only known after this bar. Liquidate at the next
                # open, including its gap and costs, and latch the circuit stop.
                pending_orders = self.portfolio.account_drawdown_orders(
                    latest_prices, broker, self.config.max_drawdown_stop_pct
                )
                snapshots.append(snapshot)
                continue

            signal_histories = {
                symbol: history
                for symbol, history in histories.items()
                if history and history[-1].date == current_date
            }
            signals = [
                self.advisor.review(signal, signal_histories[signal.symbol])
                for signal in generate_strategy_signals(self.strategy, signal_histories)
                if signal.symbol not in stopped_symbols
            ]

            if drawdown < self.config.max_drawdown_stop_pct:
                plan = self.portfolio.build_order_plan(
                    signals,
                    latest_prices,
                    broker,
                    snapshot.equity,
                    as_of_date=current_date,
                )
                pending_orders.extend(plan.orders)

            snapshots.append(broker.snapshot(latest_prices, current_date))

        return RunResult(snapshots=snapshots, fills=list(broker.fills))
