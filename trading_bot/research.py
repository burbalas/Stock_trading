from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from .advisor import HeuristicAdvisor
from .config import BotConfig
from .data import StaticMarketDataProvider
from .engine import RunResult, TradingEngine
from .models import Candle, PortfolioSnapshot
from .strategies import (
    EnsembleStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    MovingAverageCrossStrategy,
    RegimeRelativeStrengthStrategy,
    Strategy,
)


POPULAR_STOCKS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AVGO",
    "AMD",
    "NFLX",
    "JPM",
    "V",
    "MA",
    "COST",
    "WMT",
]


@dataclass(frozen=True)
class StrategySpec:
    name: str
    short_window: int
    long_window: int
    min_confidence: float
    strategy: Strategy


@dataclass(frozen=True)
class EvaluationMetrics:
    final_equity: float
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    score: float


@dataclass(frozen=True)
class EvaluationRow:
    spec: StrategySpec
    train: EvaluationMetrics
    test: EvaluationMetrics
    benchmark_return_pct: float


@dataclass(frozen=True)
class WalkForwardRow:
    spec_name: str
    folds: int
    positive_folds: int
    avg_test_return_pct: float
    avg_benchmark_return_pct: float
    avg_edge_pct: float
    worst_drawdown_pct: float
    avg_sharpe: float
    total_trades: int
    avg_score: float


def build_strategy_specs() -> list[StrategySpec]:
    specs: list[StrategySpec] = []
    for short, long in [(5, 20), (10, 30), (20, 50)]:
        specs.append(
            StrategySpec(
                name=f"ma-{short}-{long}",
                short_window=short,
                long_window=long,
                min_confidence=0.55,
                strategy=MovingAverageCrossStrategy(short, long),
            )
        )
    for lookback in [20, 40, 60]:
        specs.append(
            StrategySpec(
                name=f"momentum-{lookback}",
                short_window=max(5, lookback // 3),
                long_window=lookback,
                min_confidence=0.58,
                strategy=MomentumStrategy(lookback=lookback),
            )
        )
    for window in [20, 40, 60]:
        specs.append(
            StrategySpec(
                name=f"mean-reversion-{window}",
                short_window=max(5, window // 3),
                long_window=window,
                min_confidence=0.58,
                strategy=MeanReversionStrategy(window=window),
            )
        )
    for short, long in [(5, 20), (10, 30), (20, 50)]:
        specs.append(
            StrategySpec(
                name=f"ensemble-{short}-{long}",
                short_window=short,
                long_window=long,
                min_confidence=0.62,
                strategy=EnsembleStrategy(
                    [
                        MovingAverageCrossStrategy(short, long),
                        MomentumStrategy(lookback=long),
                        MeanReversionStrategy(window=long),
                    ]
                ),
            )
        )
    specs.append(
        StrategySpec(
            name="relative-strength-189-100-b60",
            short_window=20,
            long_window=100,
            min_confidence=0.68,
            strategy=RegimeRelativeStrengthStrategy(),
        )
    )
    return specs


def build_expanded_strategy_specs() -> list[StrategySpec]:
    specs: list[StrategySpec] = []
    seen: set[str] = set()

    def add(spec: StrategySpec) -> None:
        if spec.name in seen:
            return
        seen.add(spec.name)
        specs.append(spec)

    for short, long in [(3, 10), (5, 15), (5, 20), (8, 21), (10, 30), (15, 45), (20, 50), (30, 90)]:
        add(
            StrategySpec(
                name=f"ma-{short}-{long}",
                short_window=short,
                long_window=long,
                min_confidence=0.55,
                strategy=MovingAverageCrossStrategy(short, long),
            )
        )

    for lookback in [10, 15, 20, 30, 40, 60, 90, 120]:
        for threshold in [0.03, 0.04, 0.06, 0.08]:
            threshold_label = int(threshold * 100)
            add(
                StrategySpec(
                    name=f"momentum-{lookback}-t{threshold_label}",
                    short_window=max(5, lookback // 3),
                    long_window=lookback,
                    min_confidence=0.58,
                    strategy=MomentumStrategy(lookback=lookback, threshold_pct=threshold),
                )
            )

    for window in [15, 20, 30, 40, 60, 90]:
        for threshold in [0.04, 0.06, 0.08, 0.10]:
            threshold_label = int(threshold * 100)
            add(
                StrategySpec(
                    name=f"mean-reversion-{window}-t{threshold_label}",
                    short_window=max(5, window // 3),
                    long_window=window,
                    min_confidence=0.58,
                    strategy=MeanReversionStrategy(window=window, threshold_pct=threshold),
                )
            )

    for short, long in [(3, 10), (5, 15), (5, 20), (8, 21), (10, 30), (15, 45), (20, 50), (30, 90)]:
        for confidence in [0.60, 0.65, 0.70]:
            confidence_label = int(confidence * 100)
            add(
                StrategySpec(
                    name=f"ensemble-{short}-{long}-c{confidence_label}",
                    short_window=short,
                    long_window=long,
                    min_confidence=confidence,
                    strategy=EnsembleStrategy(
                        [
                            MovingAverageCrossStrategy(short, long),
                            MomentumStrategy(lookback=long),
                            MeanReversionStrategy(window=long),
                        ]
                    ),
                )
            )

    for momentum_window in [63, 126, 189]:
        for trend_window in [100, 150]:
            for min_breadth in [0.50, 0.60]:
                breadth_label = int(min_breadth * 100)
                add(
                    StrategySpec(
                        name=f"relative-strength-{momentum_window}-{trend_window}-b{breadth_label}",
                        short_window=20,
                        long_window=trend_window,
                        min_confidence=0.68,
                        strategy=RegimeRelativeStrengthStrategy(
                            momentum_window=momentum_window,
                            trend_window=trend_window,
                            min_breadth=min_breadth,
                        ),
                    )
                )

    return specs


def evaluate_specs(
    data: dict[str, list[Candle]],
    symbols: list[str],
    base_config: BotConfig,
    specs: list[StrategySpec] | None = None,
    train_pct: float = 0.65,
) -> list[EvaluationRow]:
    specs = specs or build_strategy_specs()
    train_data, test_data = split_market_data(data, train_pct)
    all_dates = sorted({candle.date for rows in data.values() for candle in rows})
    split_at = max(1, min(len(all_dates) - 1, int(len(all_dates) * train_pct)))
    test_start_date = all_dates[split_at]
    rows: list[EvaluationRow] = []

    for spec in specs:
        config = BotConfig(
            symbols=symbols,
            starting_cash=base_config.starting_cash,
            max_position_pct=base_config.max_position_pct,
            max_order_pct=base_config.max_order_pct,
            max_open_positions=base_config.max_open_positions,
            max_daily_orders=base_config.max_daily_orders,
            min_trade_value=base_config.min_trade_value,
            target_cash_pct=base_config.target_cash_pct,
            stop_loss_pct=base_config.stop_loss_pct,
            trailing_stop_pct=base_config.trailing_stop_pct,
            slippage_bps=base_config.slippage_bps,
            commission_per_trade=base_config.commission_per_trade,
            max_drawdown_stop_pct=base_config.max_drawdown_stop_pct,
            min_confidence=spec.min_confidence,
            short_window=spec.short_window,
            long_window=spec.long_window,
            allow_position_adds=base_config.allow_position_adds,
            buy_cooldown_days=base_config.buy_cooldown_days,
        )
        train_result = _run_static(train_data, config, spec.strategy)
        test_result = _run_static(test_data, config, spec.strategy, trade_start_date=test_start_date)
        benchmark_return = buy_and_hold_return(test_data, base_config.starting_cash, start_date=test_start_date)
        rows.append(
            EvaluationRow(
                spec=spec,
                train=metrics(train_result),
                test=metrics(test_result),
                benchmark_return_pct=benchmark_return,
            )
        )

    return sorted(rows, key=lambda row: row.test.score, reverse=True)


def evaluate_walk_forward(
    data: dict[str, list[Candle]],
    symbols: list[str],
    base_config: BotConfig,
    specs: list[StrategySpec] | None = None,
    folds: int = 4,
    initial_train_pct: float = 0.45,
) -> list[WalkForwardRow]:
    specs = specs or build_strategy_specs()
    all_dates = sorted({candle.date for rows in data.values() for candle in rows})
    if len(all_dates) < 120:
        raise ValueError("Walk-forward evaluation needs at least 120 market dates.")

    start_index = int(len(all_dates) * initial_train_pct)
    remaining = len(all_dates) - start_index
    step = max(20, remaining // folds)
    rows: list[WalkForwardRow] = []

    for spec in specs:
        test_returns: list[float] = []
        benchmark_returns: list[float] = []
        drawdowns: list[float] = []
        sharpes: list[float] = []
        scores: list[float] = []
        trades = 0

        for fold in range(folds):
            train_end_index = start_index + fold * step
            test_end_index = min(len(all_dates), train_end_index + step)
            if train_end_index >= len(all_dates) - 1:
                break

            train_end = all_dates[train_end_index]
            test_end = all_dates[test_end_index - 1]
            test_data = _slice_between_with_warmup(data, all_dates, train_end_index, test_end)

            config = BotConfig(
                symbols=symbols,
                starting_cash=base_config.starting_cash,
                max_position_pct=base_config.max_position_pct,
                max_order_pct=base_config.max_order_pct,
                max_open_positions=base_config.max_open_positions,
                max_daily_orders=base_config.max_daily_orders,
                min_trade_value=base_config.min_trade_value,
                target_cash_pct=base_config.target_cash_pct,
                stop_loss_pct=base_config.stop_loss_pct,
                trailing_stop_pct=base_config.trailing_stop_pct,
                slippage_bps=base_config.slippage_bps,
                commission_per_trade=base_config.commission_per_trade,
                max_drawdown_stop_pct=base_config.max_drawdown_stop_pct,
                min_confidence=spec.min_confidence,
                short_window=spec.short_window,
                long_window=spec.long_window,
                allow_position_adds=base_config.allow_position_adds,
                buy_cooldown_days=base_config.buy_cooldown_days,
            )
            test_start_date = all_dates[train_end_index]
            test_result = _run_static(test_data, config, spec.strategy, trade_start_date=test_start_date)
            test_metrics = metrics(test_result)
            benchmark_return = buy_and_hold_return(
                test_data,
                base_config.starting_cash,
                start_date=test_start_date,
            )

            test_returns.append(test_metrics.return_pct)
            benchmark_returns.append(benchmark_return)
            drawdowns.append(test_metrics.max_drawdown_pct)
            sharpes.append(test_metrics.sharpe)
            scores.append(test_metrics.score)
            trades += test_metrics.trades

        if not test_returns:
            continue

        edges = [test - bench for test, bench in zip(test_returns, benchmark_returns)]
        rows.append(
            WalkForwardRow(
                spec_name=spec.name,
                folds=len(test_returns),
                positive_folds=sum(1 for edge in edges if edge > 0),
                avg_test_return_pct=sum(test_returns) / len(test_returns),
                avg_benchmark_return_pct=sum(benchmark_returns) / len(benchmark_returns),
                avg_edge_pct=sum(edges) / len(edges),
                worst_drawdown_pct=min(drawdowns),
                avg_sharpe=sum(sharpes) / len(sharpes),
                total_trades=trades,
                avg_score=sum(scores) / len(scores),
            )
        )

    return sorted(rows, key=lambda row: (row.positive_folds, row.avg_score), reverse=True)


def split_market_data(data: dict[str, list[Candle]], train_pct: float) -> tuple[dict[str, list[Candle]], dict[str, list[Candle]]]:
    train: dict[str, list[Candle]] = {}
    test: dict[str, list[Candle]] = {}

    for symbol, rows in data.items():
        if not rows:
            train[symbol] = []
            test[symbol] = []
            continue
        split_at = max(1, min(len(rows) - 1, int(len(rows) * train_pct)))
        warmup_start = max(0, split_at - 260)
        train[symbol] = rows[:split_at]
        test[symbol] = rows[warmup_start:]

    return train, test


def _slice_until(data: dict[str, list[Candle]], end_date) -> dict[str, list[Candle]]:
    return {
        symbol: [candle for candle in rows if candle.date <= end_date]
        for symbol, rows in data.items()
    }


def _slice_between_with_warmup(
    data: dict[str, list[Candle]],
    all_dates,
    start_index: int,
    end_date,
    warmup: int = 260,
) -> dict[str, list[Candle]]:
    warmup_start = all_dates[max(0, start_index - warmup)]
    return {
        symbol: [candle for candle in rows if warmup_start <= candle.date <= end_date]
        for symbol, rows in data.items()
    }


def metrics(result: RunResult) -> EvaluationMetrics:
    returns = _daily_returns(result.snapshots)
    max_drawdown = _max_drawdown(result.snapshots)
    sharpe = _sharpe(returns)
    trade_penalty = min(5.0, len(result.fills) * 0.02)
    score = result.return_pct - abs(max_drawdown) * 0.65 + sharpe * 2 - trade_penalty
    return EvaluationMetrics(
        final_equity=result.final_equity,
        return_pct=result.return_pct,
        max_drawdown_pct=max_drawdown,
        sharpe=sharpe,
        trades=len(result.fills),
        score=score,
    )


def buy_and_hold_return(
    data: dict[str, list[Candle]],
    starting_cash: float,
    start_date=None,
) -> float:
    filtered = [
        [candle for candle in rows if start_date is None or candle.date >= start_date]
        for rows in data.values()
    ]
    usable = [rows for rows in filtered if len(rows) >= 2]
    if not usable:
        return 0.0

    allocation = starting_cash / len(usable)
    final_value = 0.0
    for rows in usable:
        first = rows[0].close
        last = rows[-1].close
        if first <= 0:
            continue
        shares = allocation / first
        final_value += shares * last
    return (final_value - starting_cash) / starting_cash * 100


def _run_static(
    data: dict[str, list[Candle]],
    config: BotConfig,
    strategy: Strategy,
    trade_start_date=None,
) -> RunResult:
    engine = TradingEngine(
        config=config,
        data_provider=StaticMarketDataProvider(data),
        strategy=strategy,
        advisor=HeuristicAdvisor(),
        trade_start_date=trade_start_date,
    )
    return engine.run()


def _daily_returns(snapshots: list[PortfolioSnapshot]) -> list[float]:
    if len(snapshots) < 2:
        return []
    returns: list[float] = []
    for previous, current in zip(snapshots, snapshots[1:]):
        if previous.equity > 0:
            returns.append((current.equity - previous.equity) / previous.equity)
    return returns


def _max_drawdown(snapshots: list[PortfolioSnapshot]) -> float:
    peak = 0.0
    worst = 0.0
    for snapshot in snapshots:
        peak = max(peak, snapshot.equity)
        if peak:
            drawdown = (snapshot.equity - peak) / peak * 100
            worst = min(worst, drawdown)
    return worst


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    stddev = sqrt(variance)
    if stddev == 0:
        return 0.0
    return average / stddev * sqrt(252)
