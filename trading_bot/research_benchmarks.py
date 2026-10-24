"""Offline raw-price benchmarks using the same accounting as the candidate.

Passive portfolios are feasible whole-share simulations. The explicitly named
ex-post exposure reference is a separate analytical interpolation and must never
be presented as a deployable whole-share portfolio or an independent strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from math import isfinite

from .config import BotConfig
from .data import StaticMarketDataProvider, validate_market_data
from .engine import RunResult
from .models import Candle, PortfolioSnapshot, Side, Signal
from .research_accounting import (
    CorporateActionLedger, CorporateActionResearchEngine, ResearchRunResult,
)


class _IdentityAdvisor:
    def review(self, signal: Signal, history: list[Candle]) -> Signal:
        return signal


class _InitialBuyAndHold:
    def __init__(self, start: date) -> None:
        self.start = start
        self.signaled: set[str] = set()

    def generate(self, symbol: str, history: list[Candle]) -> Signal:
        if history[-1].date >= self.start and symbol not in self.signaled:
            self.signaled.add(symbol)
            return Signal(symbol, Side.BUY, 1.0, "passive initial allocation; no rebalance")
        return Signal(symbol, Side.HOLD, 1.0, "passive hold; no dividend reinvestment")


def _finite(value: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def _validate_cash(value: float) -> None:
    if not _finite(value) or value <= 0:
        raise ValueError("starting_cash must be finite and positive.")


def _symbols(symbols: list[str]) -> list[str]:
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("Benchmark symbols must be a nonempty list.")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in symbols):
        raise ValueError("Benchmark symbols must be nonempty ticker strings without surrounding whitespace.")
    result = [item.upper() for item in symbols]
    if len(set(result)) != len(result):
        raise ValueError("Benchmark symbols must be unique.")
    return result


def run_passive_raw(data: dict[str, list[Candle]], ledger: CorporateActionLedger, *,
                    start: date, starting_cash: float, symbols: list[str], fraction: float,
                    slippage_bps: float, commission: float = 0,
                    withholding: float = 0) -> ResearchRunResult:
    """Allocate fraction equally once, using raw whole-share next-open fills.

    First signals occur at each asset's first observed evaluation close. Quantity
    is planned there and capped again at its following session's raw open; an
    upward gap can reduce quantity and a downward gap cannot enlarge it. Missing
    next-session bars expire DAY orders without retry. Cash dividends remain
    cash. Unaffordable allocations remain cash rather than creating fractions.
    """
    _validate_cash(starting_cash)
    if type(start) is not date:
        raise ValueError("Benchmark start must be a calendar date.")
    if not _finite(fraction) or not 0 < fraction <= 1:
        raise ValueError("Passive allocation fraction must be above zero and at most one; use run_cash_reference for zero.")
    selected = _symbols(symbols)
    bars = validate_market_data(data, selected)
    if not any(bar.date >= start for rows in bars.values() for bar in rows):
        raise ValueError("No benchmark bars exist on or after its start date.")
    filtered = replace(ledger,
                       splits=tuple(action for action in ledger.splits if action.symbol in selected),
                       dividends=tuple(action for action in ledger.dividends if action.symbol in selected))
    config = BotConfig(
        symbols=selected, starting_cash=starting_cash,
        max_order_pct=fraction / len(selected), max_position_pct=fraction / len(selected),
        max_open_positions=len(selected), max_daily_orders=len(selected),
        min_trade_value=0, target_cash_pct=1 - fraction,
        stop_loss_pct=.999999, trailing_stop_pct=.999999,
        max_drawdown_stop_pct=.999999, min_confidence=0,
        slippage_bps=slippage_bps, commission_per_trade=commission,
        allow_position_adds=False,
    )
    result = CorporateActionResearchEngine(
        config, StaticMarketDataProvider(bars), _InitialBuyAndHold(start),
        _IdentityAdvisor(), filtered, trade_start_date=start,
        fractional_split_policy="reject", dividend_withholding_pct=withholding,
    ).run()
    return replace(result, assumptions=result.assumptions + (
        f"Passive target total allocation {fraction:.8g}, equal per requested asset; one signal at each first evaluation close, next-open whole-share sizing.",
        "No rebalance, position adds or dividend reinvestment; idle cash earns no interest. Downward opening gaps do not increase the planned share count.",
        "Risk stops are effectively disabled (99.9999% thresholds); catastrophic near-total extinction remains an engine guard, not passive downside protection.",
    ))


def run_cash_reference(data: dict[str, list[Candle]], *, start: date,
                       starting_cash: float, symbols: list[str] | None = None) -> RunResult:
    """Zero-interest cash on precisely the supplied evaluation session grid."""
    _validate_cash(starting_cash)
    if type(start) is not date:
        raise ValueError("Benchmark start must be a calendar date.")
    selected = _symbols(symbols) if symbols is not None else list(data)
    bars = validate_market_data(data, selected)
    dates = sorted({bar.date for rows in bars.values() for bar in rows if bar.date >= start})
    if not dates:
        raise ValueError("No cash-reference bars exist on or after its start date.")
    return RunResult([PortfolioSnapshot(day, starting_cash, 0.0, starting_cash) for day in dates], [])


def average_invested_fraction(result: RunResult) -> float:
    """Arithmetic mean of daily non-cash assets / equity, including day one."""
    if not result.snapshots:
        raise ValueError("Exposure requires a nonempty result.")
    values = []
    for snapshot in result.snapshots:
        if (not _finite(snapshot.equity) or snapshot.equity <= 0
                or not _finite(snapshot.positions_value) or snapshot.positions_value < 0
                or snapshot.positions_value > snapshot.equity + 1e-8):
            raise ValueError("Exposure reference requires finite, unleveraged portfolio snapshots.")
        values.append(snapshot.positions_value / snapshot.equity)
    return sum(values) / len(values)


@dataclass(frozen=True)
class ExPostExposureReference(RunResult):
    scale: float
    candidate_average_exposure: float
    benchmark_average_exposure: float
    assumptions: tuple[str, ...]


def ex_post_exposure_reference(spy_result: RunResult, *,
                               candidate_average_exposure: float) -> ExPostExposureReference:
    """Scale a full SPY curve toward cash to match observed average exposure.

    The weight uses the candidate's entire realized evaluation period and thus
    is known only afterwards. Costs, dividends and receivables embedded in SPY
    are scaled proportionally. There are intentionally no simulated fills: the
    resulting quantities generally are not whole-share tradable allocations.
    """
    if not _finite(candidate_average_exposure) or not 0 <= candidate_average_exposure <= 1:
        raise ValueError("Candidate average exposure must be between zero and one.")
    benchmark_exposure = average_invested_fraction(spy_result)
    initial = spy_result.snapshots[0]
    if (not _finite(initial.cash) or initial.cash <= 0 or initial.positions_value != 0
            or initial.equity != initial.cash):
        raise ValueError("SPY reference must start with its initial cash-only evaluation close.")
    if benchmark_exposure <= 0:
        if candidate_average_exposure > 0:
            raise ValueError("SPY was unaffordable; a positive exposure reference cannot be constructed.")
        scale = 0.0
    elif candidate_average_exposure > benchmark_exposure + 1e-12:
        raise ValueError("Matching candidate exposure would require scaling beyond the full SPY portfolio.")
    else:
        # Equity changes with the cash mixture, so merely dividing the two mean
        # exposures is only an approximation. Solve against the mixed curve.
        lower, upper = 0.0, 1.0
        for _ in range(80):
            middle = (lower + upper) / 2
            exposure = sum(
                middle * item.positions_value
                / (initial.cash + middle * (item.equity - initial.cash))
                for item in spy_result.snapshots
            ) / len(spy_result.snapshots)
            if exposure < candidate_average_exposure:
                lower = middle
            else:
                upper = middle
        scale = (lower + upper) / 2
    snapshots = [
        PortfolioSnapshot(item.date, initial.cash + scale * (item.cash - initial.cash),
                          scale * item.positions_value,
                          initial.cash + scale * (item.equity - initial.cash))
        for item in spy_result.snapshots
    ]
    return ExPostExposureReference(snapshots, [], scale, candidate_average_exposure,
                                  benchmark_exposure, (
        "Analytical ex-post exposure reference only; allocation weight depends on the candidate's whole observed evaluation period.",
        "Linear interpolation of actual whole-share SPY and zero-interest cash; no executable fractional positions or fills are asserted.",
        "Embedded SPY execution costs, dividends and receivables scale proportionally; this is not an independently costed portfolio simulation.",
        "Mean daily non-cash asset fraction includes the initial cash-only close and any dividend receivables.",
    ))
