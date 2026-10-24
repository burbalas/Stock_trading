from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite, sqrt

from .advisor import HeuristicAdvisor
from .config import BotConfig
from .data import StaticMarketDataProvider, validate_market_data
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


def _strategy_parameters(strategy) -> dict:
    parameters = {}
    for name, value in vars(strategy).items():
        if name.startswith("_"):
            continue
        if name == "strategies":
            parameters[name] = [_strategy_parameters(child) for child in value]
        elif isinstance(value, (str, int, float, bool, type(None))):
            parameters[name] = value
    return {"class": type(strategy).__name__, **parameters}


def build_research_manifest(
    data: dict[str, list[Candle]],
    symbols: list[str],
    base_config: BotConfig,
    specs: list[StrategySpec],
    *,
    data_source: str,
    train_pct: float = 0.65,
    folds: int | None = None,
) -> dict:
    """Record reproducible inputs without turning exploration into proof.

    Callers can append output-file SHA256 values after writing their reports.
    A manifest records provenance; it is not an independent audit or attestation
    that historical data was never examined during strategy development.
    """
    data = validate_market_data(data, symbols)
    base_config.validate()
    all_dates = sorted({bar.date for rows in data.values() for bar in rows})
    bars = [
        [symbol, bar.date.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
        for symbol, rows in sorted(data.items()) for bar in rows
    ]
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "data_source": data_source,
        "data": {
            "sha256": sha256(json.dumps(bars, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "start_date": all_dates[0].isoformat(),
            "end_date": all_dates[-1].isoformat(),
            "symbols": sorted(data),
            "bar_counts": {symbol: len(rows) for symbol, rows in sorted(data.items())},
            "market_dates": len(all_dates),
            "future_dated": all_dates[-1] > now.date(),
        },
        "config": asdict(base_config),
        "strategies": [
            {"name": spec.name, "short_window": spec.short_window, "long_window": spec.long_window,
             "min_confidence": spec.min_confidence, "parameters": _strategy_parameters(spec.strategy)}
            for spec in specs
        ],
        "evaluation": {
            "train_pct": train_pct,
            "folds": folds,
            "ranking_basis": "training_score",
            "walk_forward_method": "fixed_candidate_disjoint_windows",
            "signal_execution": "next_session_open",
            "benchmark": "equal_weight_next_open_with_entry_costs",
            "sharpe_risk_free_rate": 0.0,
        },
        "evaluation_type": "exploratory",
        "profitability_proven": False,
        "holdout_verified": False,
        "limitations": [
            "Repeated holdout inspection and ranking walk-forward aggregates introduce selection bias.",
            "The requested universe may contain survivorship or hindsight selection bias.",
            "Corporate-action adjustments and dividends require independent source verification.",
            "Daily OHLC cannot reproduce intraday execution, spreads, liquidity, or stop paths exactly.",
            "Missing bars are marked at the last price; unavailable next-session DAY orders expire.",
            "Backtest and paper results do not establish future profitability or live execution quality.",
        ],
    }


def assess_research_evidence(
    research_rows: list[dict[str, str]],
    walk_rows: list[dict[str, str]],
    *,
    manifest: dict | None = None,
) -> dict:
    """Screen research for a paper-study candidate, never live authorization.

    Input row order must be the training-only ranking written by evaluate_specs.
    Report-file hashes must be checked by the caller against the manifest.
    """
    blockers: list[str] = []
    insufficient = False
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        blockers.append("Missing supported research provenance manifest.")
        insufficient = True
        manifest = {}
    sections = {}
    for name in ("evaluation", "data", "config"):
        value = manifest.get(name, {})
        if not isinstance(value, dict):
            blockers.append(f"Invalid provenance section: {name}.")
            insufficient = True
            value = {}
        sections[name] = value
    evaluation = sections["evaluation"]
    if evaluation.get("ranking_basis") != "training_score":
        blockers.append("Candidate selection was not recorded as training-only.")
        insufficient = True
    if manifest.get("data_source") == "demo":
        blockers.append("Synthetic demo data cannot establish market profitability.")
        insufficient = True
    if sections["data"].get("future_dated"):
        blockers.append("Research includes future-dated market data.")
        insufficient = True
    candidate = research_rows[0].get("strategy") if research_rows else None
    if not isinstance(candidate, str) or not candidate.strip():
        blockers.append("Missing candidate strategy name.")
        insufficient = True
    row = research_rows[0] if research_rows else {}
    walk = next((item for item in walk_rows if item.get("strategy") == candidate), {})
    if not row:
        blockers.append("Missing chronological holdout results.")
        insufficient = True
    if not walk:
        blockers.append("Missing matching walk-forward results.")
        insufficient = True
    parsed: dict[str, float] = {}
    required_metrics = (
        ("test_return_pct", row), ("benchmark_return_pct", row),
        ("test_max_drawdown_pct", row), ("test_trades", row),
        ("folds", walk), ("positive_folds", walk), ("avg_edge_pct", walk),
        ("worst_drawdown_pct", walk), ("total_trades", walk),
    )
    for key, source in required_metrics:
        try:
            value = float(source[key])
            if not isfinite(value):
                raise ValueError
            if key in {"folds", "positive_folds", "test_trades", "total_trades"} and (value < 0 or value != int(value)):
                raise ValueError
            parsed[key] = value
        except (KeyError, ValueError, TypeError):
            blockers.append(f"Missing or invalid metric: {key}.")
            insufficient = True
    if len(parsed) == len(required_metrics):
        parsed["test_edge_pct"] = parsed["test_return_pct"] - parsed["benchmark_return_pct"]
        if parsed["test_return_pct"] <= 0:
            blockers.append("Holdout return is not positive after modeled costs.")
        if parsed["test_edge_pct"] <= 0:
            blockers.append("Holdout does not outperform the benchmark after modeled costs.")
        if parsed["avg_edge_pct"] <= 0:
            blockers.append("Average walk-forward edge is not positive.")
        if parsed["folds"] < 4:
            blockers.append("At least four walk-forward folds are required for screening.")
        if not 0 <= parsed["positive_folds"] <= parsed["folds"]:
            blockers.append("Invalid positive-fold count.")
            insufficient = True
        elif parsed["positive_folds"] < parsed["folds"] * 0.75:
            blockers.append("Fewer than 75% of walk-forward folds outperform the benchmark.")
        if parsed["test_trades"] < 20 or parsed["total_trades"] < 20:
            blockers.append("Fewer than 20 fills provide insufficient trading observations.")
        try:
            max_drawdown = float(sections["config"].get("max_drawdown_stop_pct", 0.15)) * 100
            if not isfinite(max_drawdown) or not 0 < max_drawdown < 100:
                raise ValueError
        except (ValueError, TypeError):
            max_drawdown = 15.0
            blockers.append("Invalid configured drawdown threshold.")
            insufficient = True
        if parsed["test_max_drawdown_pct"] > 0 or parsed["worst_drawdown_pct"] > 0:
            blockers.append("Drawdown metrics must use nonpositive percentages.")
            insufficient = True
        if min(parsed["test_max_drawdown_pct"], parsed["worst_drawdown_pct"]) < -max_drawdown:
            blockers.append("Observed drawdown exceeds the configured circuit-stop threshold.")
    return {
        "status": "insufficient_evidence" if insufficient else "fails_research_checks" if blockers else "research_candidate",
        "candidate": candidate,
        "blockers": blockers,
        "metrics": parsed,
        "profitability_proven": False,
        "eligible_for_live": False,
        "exploratory_only": True,
    }


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
    """Evaluate a chronological holdout, ranking candidates on training only.

    Inspecting the holdout repeatedly still turns it into selection data. This
    report is exploratory and does not attest to an untouched future holdout.
    """
    specs = build_strategy_specs() if specs is None else specs
    data = validate_market_data(data, symbols)
    train_data, test_data = split_market_data(data, train_pct)
    all_dates = sorted({candle.date for rows in data.values() for candle in rows})
    split_at = max(1, min(len(all_dates) - 1, int(len(all_dates) * train_pct)))
    test_start_date = all_dates[split_at]
    rows: list[EvaluationRow] = []

    for spec in specs:
        config = replace(
            base_config,
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
        benchmark_return = buy_and_hold_return(
            test_data, base_config.starting_cash, start_date=test_start_date,
            slippage_bps=base_config.slippage_bps,
            commission_per_trade=base_config.commission_per_trade,
        )
        rows.append(
            EvaluationRow(
                spec=spec,
                train=metrics(train_result),
                test=metrics(test_result),
                benchmark_return_pct=benchmark_return,
            )
        )

    return sorted(rows, key=lambda row: row.train.score, reverse=True)


def evaluate_walk_forward(
    data: dict[str, list[Candle]],
    symbols: list[str],
    base_config: BotConfig,
    specs: list[StrategySpec] | None = None,
    folds: int = 4,
    initial_train_pct: float = 0.45,
) -> list[WalkForwardRow]:
    """Compare fixed candidates across disjoint windows; this is exploration.

    Ranking these aggregates is parameter selection, not a separately verified
    out-of-sample result for the winner.
    """
    specs = build_strategy_specs() if specs is None else specs
    if isinstance(folds, bool) or not isinstance(folds, int) or folds < 1:
        raise ValueError("folds must be a positive integer.")
    if not 0 < initial_train_pct < 1:
        raise ValueError("initial_train_pct must be between 0 and 1.")
    data = validate_market_data(data, symbols)
    all_dates = sorted({candle.date for rows in data.values() for candle in rows})
    if len(all_dates) < 120:
        raise ValueError("Walk-forward evaluation needs at least 120 market dates.")

    start_index = int(len(all_dates) * initial_train_pct)
    remaining = len(all_dates) - start_index
    if start_index < 1 or remaining < folds * 20:
        raise ValueError("Each walk-forward fold needs at least 20 test market dates.")
    rows: list[WalkForwardRow] = []

    for spec in specs:
        test_returns: list[float] = []
        benchmark_returns: list[float] = []
        drawdowns: list[float] = []
        sharpes: list[float] = []
        scores: list[float] = []
        trades = 0

        for fold in range(folds):
            train_end_index = start_index + remaining * fold // folds
            test_end_index = start_index + remaining * (fold + 1) // folds
            test_end = all_dates[test_end_index - 1]
            test_data = _slice_between_with_warmup(data, all_dates, train_end_index, test_end)

            config = replace(
                base_config,
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
                slippage_bps=base_config.slippage_bps,
                commission_per_trade=base_config.commission_per_trade,
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
    if not 0 < train_pct < 1:
        raise ValueError("train_pct must be between 0 and 1.")
    data = validate_market_data(data)
    all_dates = sorted({candle.date for rows in data.values() for candle in rows})
    if len(all_dates) < 2:
        raise ValueError("Research needs at least two market dates.")
    split_at = max(1, min(len(all_dates) - 1, int(len(all_dates) * train_pct)))
    test_start = all_dates[split_at]
    warmup_start = all_dates[max(0, split_at - 260)]
    train = {symbol: [bar for bar in rows if bar.date < test_start] for symbol, rows in data.items()}
    test = {symbol: [bar for bar in rows if bar.date >= warmup_start] for symbol, rows in data.items()}
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
    slippage_bps: float = 0.0,
    commission_per_trade: float = 0.0,
) -> float:
    """Equal-weight benchmark entered at the same next open as the strategy.

    Allocations unavailable at that opening remain cash. Fractional benchmark
    shares are permitted; terminal holdings are marked to close as in the bot.
    """
    if not isfinite(starting_cash) or starting_cash <= 0:
        raise ValueError("starting_cash must be finite and positive.")
    if not isfinite(slippage_bps) or not 0 <= slippage_bps < 10_000:
        raise ValueError("slippage_bps must be finite and between 0 and 10000.")
    if not isfinite(commission_per_trade) or commission_per_trade < 0:
        raise ValueError("commission_per_trade must be finite and non-negative.")
    data = validate_market_data(data)
    all_dates = sorted({bar.date for rows in data.values() for bar in rows if start_date is None or bar.date >= start_date})
    if len(all_dates) < 2 or not data:
        return 0.0
    entry_date = all_dates[1]
    allocation = starting_cash / len(data)
    final_value = 0.0
    for rows in data.values():
        entry = next((bar for bar in rows if bar.date == entry_date), None)
        if entry is None or allocation <= commission_per_trade:
            final_value += allocation
            continue
        shares = (allocation - commission_per_trade) / (entry.open * (1 + slippage_bps / 10_000))
        final_value += shares * rows[-1].close
    return (final_value - starting_cash) / starting_cash * 100


def _run_static(
    data: dict[str, list[Candle]],
    config: BotConfig,
    strategy: Strategy,
    trade_start_date=None,
) -> RunResult:
    # A newly listed symbol may have no training bars. It contributes no orders
    # until it actually has data, without reading its later holdout history.
    config = replace(config, symbols=[symbol for symbol in config.symbols if data.get(symbol.upper())])
    engine = TradingEngine(
        config=config,
        data_provider=StaticMarketDataProvider(data),
        strategy=deepcopy(strategy),
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
