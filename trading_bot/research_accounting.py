"""Offline, point-in-time corporate-action accounting for raw-price research.

This module never connects to a broker. Execution and whole-share sizing use raw
tradable prices. Only indicator histories are split adjusted, and an adjustment
is applied when its effective date is reached, never before. An explicitly
complete action ledger is required; an empty list is not evidence of no actions.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from math import isfinite
from typing import Literal

from .advisor import Advisor
from .broker import PaperBroker
from .config import BotConfig
from .data import MarketDataProvider, validate_market_data
from .engine import RunResult
from .models import Candle, Fill, Order, PortfolioSnapshot, Side
from .portfolio import PortfolioManager
from .strategies import Strategy, generate_strategy_signals


@dataclass(frozen=True)
class SplitEvent:
    symbol: str
    effective_date: date
    new_shares: int
    old_shares: int

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _day(self.effective_date)
        for value in (self.new_shares, self.old_shares):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Split share counts must be positive integers.")
        if self.new_shares == self.old_shares:
            raise ValueError("A split ratio must change the share count.")

    @property
    def ratio(self) -> Fraction:
        return Fraction(self.new_shares, self.old_shares)


@dataclass(frozen=True)
class DividendEvent:
    symbol: str
    ex_date: date
    pay_date: date
    amount_per_share: float
    currency: str = "USD"

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _day(self.ex_date)
        _day(self.pay_date)
        if self.pay_date < self.ex_date:
            raise ValueError("A dividend cannot be payable before its ex-date.")
        if not _positive(self.amount_per_share):
            raise ValueError("Dividend amount must be finite and positive.")
        if self.currency != "USD":
            raise ValueError("Research accounting currently requires USD actions and cash.")


@dataclass(frozen=True)
class CorporateActionLedger:
    coverage_start: date
    coverage_end: date
    source: str
    complete: bool = False
    splits: tuple[SplitEvent, ...] = ()
    dividends: tuple[DividendEvent, ...] = ()

    def validate(self, data: dict[str, list[Candle]]) -> None:
        _day(self.coverage_start)
        _day(self.coverage_end)
        if self.coverage_end < self.coverage_start:
            raise ValueError("Invalid corporate-action coverage interval.")
        if self.complete is not True or not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Explicit complete corporate-action coverage and its source are required.")
        dates = {symbol: {bar.date for bar in rows} for symbol, rows in data.items()}
        all_dates = {day for days in dates.values() for day in days}
        if min(all_dates) < self.coverage_start or max(all_dates) > self.coverage_end:
            raise ValueError("Corporate-action ledger does not cover all supplied raw bars, including warm-up.")
        seen: set[tuple[str, str, date]] = set()
        split_days: set[tuple[str, date]] = set()
        for action in (*self.splits, *self.dividends):
            if not isinstance(action, (SplitEvent, DividendEvent)):
                raise ValueError("Unsupported corporate action.")
            day = action.effective_date if isinstance(action, SplitEvent) else action.ex_date
            if action.symbol not in dates:
                raise ValueError(f"Action for unrequested symbol {action.symbol}.")
            if not self.coverage_start <= day <= self.coverage_end:
                raise ValueError("Corporate action is outside the declared coverage interval.")
            key = (type(action).__name__, action.symbol, day)
            if key in seen:
                raise ValueError("Duplicate corporate action.")
            seen.add(key)
            if min(all_dates) <= day <= max(all_dates) and day not in dates[action.symbol]:
                raise ValueError("An effective action date lacks its raw execution bar.")
            if isinstance(action, SplitEvent):
                split_days.add((action.symbol, day))
        if any((item.symbol, item.ex_date) in split_days for item in self.dividends):
            raise ValueError("Same-day split/dividend share basis requires explicit accounting; unsupported.")


@dataclass(frozen=True)
class CorporateCashFlow:
    date: date
    symbol: str
    kind: str
    amount: float


@dataclass(frozen=True)
class DividendReceivable:
    symbol: str
    ex_date: date
    pay_date: date
    quantity: int
    amount: float


@dataclass(frozen=True)
class ResearchRunResult(RunResult):
    corporate_cash_flows: tuple[CorporateCashFlow, ...]
    unpaid_dividends: tuple[DividendReceivable, ...]
    daily_dividend_receivables: tuple[tuple[date, float], ...]
    assumptions: tuple[str, ...]
    decision_rejections: tuple[tuple[date, str], ...] = ()
    circuit_dates: tuple[date, ...] = ()


def _symbol(value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip().upper():
        raise ValueError("Action symbols must be nonempty uppercase tickers.")


def _day(value: date) -> None:
    if type(value) is not date:
        raise ValueError("Action dates must be calendar dates.")


def _positive(value: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value) and value > 0


def _adjust_bar(bar: Candle, ratio: Fraction) -> Candle:
    factor = float(ratio)
    # Fractional volume after a reverse split is rounded only for indicators;
    # execution volume and all raw OHLC remain untouched.
    return replace(bar, open=bar.open / factor, high=bar.high / factor,
                   low=bar.low / factor, close=bar.close / factor,
                   volume=round(bar.volume * factor))


def split_adjusted_history(raw_history: list[Candle], splits: tuple[SplitEvent, ...],
                           as_of: date) -> list[Candle]:
    """Return only bars observable by as_of, using only already-effective splits."""
    _day(as_of)
    history = [bar for bar in raw_history if bar.date <= as_of]
    for action in sorted(splits, key=lambda item: item.effective_date):
        if action.effective_date <= as_of:
            history = [_adjust_bar(bar, action.ratio)
                       if bar.symbol == action.symbol and bar.date < action.effective_date else bar
                       for bar in history]
    return history


class CorporateActionResearchEngine:
    """Same strategy/risk planning as TradingEngine, with explicit cash events.

    Bar-stop timing remains conservative: today's high cannot raise a stop that
    is then triggered by today's low. Signals fill at the following available
    session open; missing-symbol DAY orders expire. No live execution is used.
    """

    def __init__(self, config: BotConfig, raw_data_provider: MarketDataProvider,
                 strategy: Strategy, advisor: Advisor, ledger: CorporateActionLedger,
                 trade_start_date: date | None = None, *,
                 fractional_split_policy: Literal["reject", "cash_at_open"] = "reject",
                 dividend_withholding_pct: float = 0.0) -> None:
        config.validate()
        if fractional_split_policy not in ("reject", "cash_at_open"):
            raise ValueError("Unsupported fractional split policy.")
        if (isinstance(dividend_withholding_pct, bool)
                or not isinstance(dividend_withholding_pct, (int, float))
                or not isfinite(dividend_withholding_pct)
                or not 0 <= dividend_withholding_pct <= 1):
            raise ValueError("Dividend withholding must be between zero and one.")
        self.config = config
        self.data_provider = raw_data_provider
        self.strategy = strategy
        self.advisor = advisor
        self.ledger = ledger
        self.trade_start_date = trade_start_date
        self.fractional_split_policy = fractional_split_policy
        self.dividend_withholding_pct = dividend_withholding_pct

    def run(self) -> ResearchRunResult:
        data = validate_market_data(self.data_provider.load(self.config.symbols), self.config.symbols)
        self.ledger.validate(data)
        portfolio = PortfolioManager(self.config)
        broker = PaperBroker(self.config.starting_cash, self.config.slippage_bps,
                             self.config.commission_per_trade)
        by_date: dict[date, list[Candle]] = defaultdict(list)
        for rows in data.values():
            for bar in rows:
                by_date[bar.date].append(bar)
        splits: dict[date, list[SplitEvent]] = defaultdict(list)
        dividends: dict[date, list[DividendEvent]] = defaultdict(list)
        for action in self.ledger.splits:
            splits[action.effective_date].append(action)
        for action in self.ledger.dividends:
            dividends[action.ex_date].append(action)
        histories: dict[str, list[Candle]] = {symbol: [] for symbol in data}
        latest: dict[str, float] = {}
        highs: dict[str, float] = {}
        pending: list[Order] = []
        receivables: list[DividendReceivable] = []
        cash_flows: list[CorporateCashFlow] = []
        daily_receivables: list[tuple[date, float]] = []
        snapshots: list[PortfolioSnapshot] = []
        peak = self.config.starting_cash
        halted = False
        rejections: list[tuple[date, str]] = []
        circuit_dates: list[date] = []

        def snapshot(day: date, marks: dict[str, float]) -> PortfolioSnapshot:
            result = broker.snapshot(marks, day)
            accrued = sum(item.amount for item in receivables)
            # Non-cash assets include declared, unpaid dividend receivables.
            return replace(result, positions_value=result.positions_value + accrued,
                           equity=result.equity + accrued)

        for day in sorted(by_date):
            candles = {bar.symbol: bar for bar in by_date[day]}
            opens = {symbol: bar.open for symbol, bar in candles.items()}

            # Entitlement belongs to the holder entering the ex-date, before any
            # ex-date open purchases or sales. Same-day split/dividend is rejected.
            for action in dividends[day]:
                held = broker.positions.get(action.symbol)
                quantity = held.quantity if held else 0
                if quantity:
                    amount = quantity * action.amount_per_share * (1 - self.dividend_withholding_pct)
                    receivables.append(DividendReceivable(action.symbol, day, action.pay_date, quantity, amount))
            remaining = []
            for item in receivables:
                if item.pay_date <= day:
                    broker.cash += item.amount
                    cash_flows.append(CorporateCashFlow(day, item.symbol, "dividend", item.amount))
                else:
                    remaining.append(item)
            receivables = remaining

            for action in splits[day]:
                symbol, ratio = action.symbol, action.ratio
                factor = float(ratio)
                histories[symbol] = [_adjust_bar(bar, ratio) for bar in histories[symbol]]
                if symbol in latest:
                    latest[symbol] /= factor
                if symbol in highs:
                    highs[symbol] /= factor
                position = broker.positions.get(symbol)
                if position and position.quantity:
                    exact_quantity = Fraction(position.quantity) * ratio
                    whole = exact_quantity.numerator // exact_quantity.denominator
                    remainder = exact_quantity - whole
                    if remainder and self.fractional_split_policy == "reject":
                        raise ValueError(f"{symbol}: split creates fractional shares; no verified cash-in-lieu policy.")
                    position.quantity = whole
                    position.average_cost = position.average_cost / factor if whole else 0.0
                    if remainder:
                        price = opens[symbol] * (1 - self.config.slippage_bps / 10_000)
                        gross = float(remainder) * price
                        amount = gross - self.config.commission_per_trade
                        if broker.cash + amount < 0:
                            raise ValueError("Modeled cash-in-lieu fees exceed available cash.")
                        broker.cash += amount
                        broker.fills.append(Fill(day, symbol, Side.SELL, float(remainder), price,
                                                 gross, "assumed fractional split cash-in-lieu at ex-date open"))
                        cash_flows.append(CorporateCashFlow(day, symbol, "assumed_cash_in_lieu", amount))
                adjusted: list[Order] = []
                for order in pending:
                    if order.symbol != symbol:
                        adjusted.append(order)
                        continue
                    quantity = (order.quantity * ratio.numerator) // ratio.denominator
                    if quantity:
                        adjusted.append(replace(order, quantity=quantity, price=order.price / factor))
                pending = adjusted

            for order in pending:
                price = opens.get(order.symbol)
                if price is None:
                    rejections.append((day, f"{order.symbol}: pending DAY order expired without an opening bar"))
                    continue
                held = broker.positions.get(order.symbol)
                held_quantity = held.quantity if held else 0
                quantity = order.quantity
                if order.side is Side.BUY:
                    equity = snapshot(day, {**latest, **opens}).equity
                    quantity = min(quantity, portfolio.max_buy_quantity(price, equity, broker.cash, held_quantity))
                    if quantity <= 0:
                        rejections.append((day, f"{order.symbol}: opening affordability recheck rounded quantity to zero"))
                        continue
                    if quantity < order.quantity:
                        rejections.append((day, f"{order.symbol}: opening affordability reduced quantity"))
                fill = broker.submit(replace(order, quantity=quantity, price=price), day)
                if fill is None:
                    rejections.append((day, f"{order.symbol}: broker model rejected pending order"))
                if fill and fill.side is Side.BUY and not held_quantity:
                    highs[order.symbol] = fill.price
            pending = []
            highs = {symbol: high for symbol, high in highs.items()
                     if broker.positions.get(symbol) and broker.positions[symbol].quantity > 0}
            for symbol, bar in candles.items():
                histories[symbol].append(bar)
                latest[symbol] = bar.close
            if self.trade_start_date is not None and day < self.trade_start_date:
                continue

            stop_orders = portfolio.bar_stop_orders(candles, broker, highs)
            stopped = {order.symbol for order in stop_orders}
            for order in stop_orders:
                broker.submit(order, day)
            highs = {
                symbol: max(highs.get(symbol, position.average_cost),
                            candles[symbol].high if symbol in candles else 0)
                for symbol, position in broker.positions.items() if position.quantity > 0
            }
            current = snapshot(day, latest)
            peak = max(peak, current.equity)
            drawdown = (peak - current.equity) / peak
            halted = halted or drawdown >= self.config.max_drawdown_stop_pct
            if halted:
                circuit_dates.append(day)
                pending = portfolio.account_drawdown_orders(latest, broker, self.config.max_drawdown_stop_pct)
            else:
                signal_histories = {symbol: bars for symbol, bars in histories.items()
                                    if bars and bars[-1].date == day}
                signals = [self.advisor.review(signal, signal_histories[signal.symbol])
                           for signal in generate_strategy_signals(self.strategy, signal_histories)
                           if signal.symbol not in stopped]
                plan = portfolio.build_order_plan(signals, latest, broker, current.equity,
                                                  as_of_date=day)
                pending = plan.orders
                rejections.extend((day, reason) for reason in plan.rejected)
            snapshots.append(snapshot(day, latest))
            daily_receivables.append((day, sum(item.amount for item in receivables)))

        assumptions = (
            "Raw OHLC for execution, sizing, stops and marks; split-only histories adjusted as of each signal date.",
            "Next-session open market execution with configured slippage and commissions; pending split quantities rounded down.",
            "Unpaid net dividends accrue as non-cash assets on ex-date; cash credited on first supplied session on/after pay-date.",
            f"Dividend withholding assumption: {self.dividend_withholding_pct:.8g}; other taxes, FX, interest and fees excluded.",
            f"Fractional split policy: {self.fractional_split_policy}; cash_at_open is an explicit modeled disposition, not verified broker settlement.",
            f"Corporate-action ledger declared complete by caller: {self.ledger.source}",
        )
        return ResearchRunResult(snapshots, list(broker.fills), tuple(cash_flows), tuple(receivables),
                                 tuple(daily_receivables), assumptions, tuple(rejections), tuple(circuit_dates))
