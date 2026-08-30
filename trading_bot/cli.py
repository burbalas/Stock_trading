from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, datetime, time as day_time
from pathlib import Path

from .alpaca_integration import AlpacaMarketDataProvider, AlpacaPaperBroker
from .advisor import HeuristicAdvisor
from .config import BotConfig
from .data import CsvMarketDataProvider, DemoMarketDataProvider, StaticMarketDataProvider
from .engine import TradingEngine
from .journal import TradeJournal
from .live import LiveTradingEngine
from .models import Fill, Order, Side
from .reconcile import reconcile_positions
from .research import (
    POPULAR_STOCKS,
    build_expanded_strategy_specs,
    build_strategy_specs,
    evaluate_specs,
    evaluate_walk_forward,
)
from .strategies import (
    EnsembleStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    MovingAverageCrossStrategy,
    RegimeRelativeStrengthStrategy,
)
from .time_utils import MARKET_TZ, format_local, format_market, now_local

PROFILE_CHOICES = ["custom", "balanced", "aggressive-research", "paper-operator"]
STRATEGY_CHOICES = ["ma", "momentum", "mean-reversion", "ensemble", "relative-strength"]


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Paper-first AI stock trading bot")
    subparsers = parser.add_subparsers(dest="command")

    def add_live_arguments(command_parser) -> None:
        command_parser.add_argument("--symbols", nargs="+", default=["AAPL", "MSFT"], help="Symbols to evaluate")
        command_parser.add_argument("--symbols-file", help="Optional file of symbols, one per line or comma-separated")
        command_parser.add_argument("--cash", type=float, default=10_000.0)
        command_parser.add_argument("--days", type=int, default=180)
        command_parser.add_argument("--feed", default="iex")
        command_parser.add_argument("--short-window", type=int, default=10)
        command_parser.add_argument("--long-window", type=int, default=30)
        command_parser.add_argument("--min-confidence", type=float, default=0.55)
        command_parser.add_argument("--strategy", choices=STRATEGY_CHOICES, default="ensemble")
        command_parser.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
        command_parser.add_argument("--max-open-positions", type=int, default=10)
        command_parser.add_argument("--max-daily-orders", type=int, default=5)
        command_parser.add_argument("--max-position-pct", type=float, default=0.25)
        command_parser.add_argument("--max-order-pct", type=float, default=0.10)
        command_parser.add_argument("--target-cash-pct", type=float, default=0.05)
        command_parser.add_argument("--stop-loss-pct", type=float, default=0.08)
        command_parser.add_argument("--slippage-bps", type=float, default=5.0)
        command_parser.add_argument("--commission", type=float, default=0.0)
        command_parser.add_argument("--max-drawdown-stop-pct", type=float, default=0.20)
        command_parser.add_argument("--trailing-stop-pct", type=float, default=0.05)
        command_parser.add_argument("--journal-dir", default="var")
        command_parser.add_argument("--allow-closed-submit", action="store_true", help="Allow queuing paper orders while market is closed")
        command_parser.add_argument("--close-buffer-minutes", type=float, default=15.0)
        command_parser.add_argument("--open-buffer-minutes", type=float, default=10.0)
        command_parser.add_argument("--allow-near-open", action="store_true")
        command_parser.add_argument("--allow-near-close", action="store_true")
        command_parser.add_argument("--sell-only", action="store_true", help="Disable new buy orders; exits remain allowed")
        command_parser.add_argument("--allow-position-adds", action="store_true", help="Allow buying more of symbols already held")
        command_parser.add_argument("--buy-cooldown-days", type=int, default=3)
        command_parser.add_argument("--submit", action="store_true", help="Actually submit generated orders to Alpaca paper")

    demo = subparsers.add_parser("demo", help="Run a quick demo backtest")
    demo.set_defaults(command="demo")

    doctor = subparsers.add_parser("doctor", help="Check local bot setup without placing orders")
    doctor.set_defaults(command="doctor")

    clock = subparsers.add_parser("clock", help="Show Alpaca market time in Lithuania time")
    clock.set_defaults(command="clock")

    report = subparsers.add_parser("daily-report", help="Write a markdown report of the current Alpaca paper state")
    report.add_argument("--journal-dir", default="var")
    report.add_argument("--output", help="Optional report path")
    report.add_argument("--report-date", help="Override the report date label, in YYYY-MM-DD format")
    report.add_argument("--stop-loss-pct", type=float, default=0.08)
    report.add_argument("--trailing-stop-pct", type=float, default=0.05)

    strategy_report = subparsers.add_parser("strategy-report", help="Write a paper strategy performance report from fills and open positions")
    strategy_report.add_argument("--journal-dir", default="var")
    strategy_report.add_argument("--output", help="Optional report path")
    strategy_report.add_argument("--benchmark", default="SPY", help="Benchmark symbol for trade-period comparison")
    strategy_report.add_argument("--days", type=int, default=760, help="Benchmark lookback days")
    strategy_report.add_argument("--feed", default="iex")

    close_audit = subparsers.add_parser("close-audit", help="Run end-of-day protect, health, daily, and strategy reports")
    close_audit.add_argument("--journal-dir", default="var")
    close_audit.add_argument("--symbols", nargs="+", default=POPULAR_STOCKS)
    close_audit.add_argument("--symbols-file")
    close_audit.add_argument("--profile", choices=PROFILE_CHOICES, default="paper-operator")
    close_audit.add_argument("--report-date", help="Override report date label, in YYYY-MM-DD format")
    close_audit.add_argument("--benchmark", default="SPY")
    close_audit.add_argument("--days", type=int, default=760)
    close_audit.add_argument("--feed", default="iex")
    close_audit.add_argument("--output", default=None, help=argparse.SUPPRESS)
    close_audit.add_argument("--stop-loss-pct", type=float, default=0.08)
    close_audit.add_argument("--trailing-stop-pct", type=float, default=0.05)
    close_audit.add_argument("--skip-tests", action="store_true")
    close_audit.add_argument("--submit", action="store_true", help="Submit/raise missing protective stops during audit")

    promote = subparsers.add_parser("promotion-report", help="Evaluate whether research evidence justifies changing live strategy")
    promote.add_argument("--research-csv", default="research_expanded_2026-07-27.csv")
    promote.add_argument("--walk-forward-csv", default="research_expanded_walkforward_2026-07-27.csv")
    promote.add_argument("--current", default="ensemble-5-20-c65")
    promote.add_argument("--output", default="var\\promotion_report.md")
    promote.add_argument("--min-positive-folds", type=int, default=3)
    promote.add_argument("--min-avg-edge-pct", type=float, default=0.0)
    promote.add_argument("--min-score-improvement", type=float, default=2.0)
    promote.add_argument("--max-worst-drawdown-pct", type=float, default=-15.0)

    risk_promote = subparsers.add_parser("risk-promotion-report", help="Evaluate whether risk research justifies changing stop settings")
    risk_promote.add_argument("--risk-csv", default="risk_research_2026-08-04.csv")
    risk_promote.add_argument("--current-stop-loss-pct", type=float, default=0.08)
    risk_promote.add_argument("--current-trailing-stop-pct", type=float, default=0.05)
    risk_promote.add_argument("--output", default="var\\risk_promotion_report.md")
    risk_promote.add_argument("--min-return-improvement-pct", type=float, default=2.0)
    risk_promote.add_argument("--min-score-improvement", type=float, default=2.0)
    risk_promote.add_argument("--max-drawdown-worsening-pct", type=float, default=1.0)

    status = subparsers.add_parser("status", help="Show Alpaca paper account, positions, orders, and local bot state")
    status.add_argument("--symbols", nargs="+", default=POPULAR_STOCKS, help="Symbols to evaluate for optional signals")
    status.add_argument("--symbols-file", help="Optional file of symbols, one per line or comma-separated")
    status.add_argument("--profile", choices=PROFILE_CHOICES, default="paper-operator")
    status.add_argument("--cash", type=float, default=100_000.0)
    status.add_argument("--days", type=int, default=180)
    status.add_argument("--feed", default="iex")
    status.add_argument("--short-window", type=int, default=10)
    status.add_argument("--long-window", type=int, default=30)
    status.add_argument("--min-confidence", type=float, default=0.55)
    status.add_argument("--strategy", choices=STRATEGY_CHOICES, default="ensemble")
    status.add_argument("--max-open-positions", type=int, default=10)
    status.add_argument("--max-daily-orders", type=int, default=5)
    status.add_argument("--max-position-pct", type=float, default=0.25)
    status.add_argument("--max-order-pct", type=float, default=0.10)
    status.add_argument("--target-cash-pct", type=float, default=0.05)
    status.add_argument("--stop-loss-pct", type=float, default=0.08)
    status.add_argument("--slippage-bps", type=float, default=5.0)
    status.add_argument("--commission", type=float, default=0.0)
    status.add_argument("--max-drawdown-stop-pct", type=float, default=0.20)
    status.add_argument("--trailing-stop-pct", type=float, default=0.05)
    status.add_argument("--sell-only", action="store_true", help="Disable new buy orders; exits remain allowed")
    status.add_argument("--allow-position-adds", action="store_true", help="Allow buying more of symbols already held")
    status.add_argument("--buy-cooldown-days", type=int, default=3)
    status.add_argument("--signals", action="store_true", help="Also run a dry latest-signal scan")
    status.add_argument("--journal-dir", default="var")
    status.set_defaults(submit=False, allow_closed_submit=False)

    reset_state = subparsers.add_parser("reset-state", help="Archive local bot runtime state and bind to current Alpaca paper account")
    reset_state.add_argument("--journal-dir", default="var")
    reset_state.add_argument("--yes", action="store_true", help="Confirm reset without prompting")

    research = subparsers.add_parser("research", help="Compare strategies on a multi-symbol universe")
    research.add_argument("--symbols", nargs="+", default=POPULAR_STOCKS, help="Symbols to evaluate")
    research.add_argument("--symbols-file", help="Optional file of symbols, one per line or comma-separated")
    research.add_argument("--data", choices=["demo", "alpaca"], default="alpaca")
    research.add_argument("--days", type=int, default=730)
    research.add_argument("--feed", default="iex")
    research.add_argument("--cash", type=float, default=100_000.0)
    research.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
    research.add_argument("--train-pct", type=float, default=0.65)
    research.add_argument("--top", type=int, default=8)
    research.add_argument("--csv-out", help="Optional CSV path for full research results")
    research.add_argument("--walk-forward", action="store_true", help="Run sequential walk-forward validation")
    research.add_argument("--walk-forward-csv-out", help="Optional CSV path for walk-forward results")
    research.add_argument("--expanded", action="store_true", help="Evaluate a broader strategy/parameter grid")
    research.add_argument("--strategy-filter", help="Comma-separated exact strategy names to evaluate")
    research.add_argument("--folds", type=int, default=4)
    research.add_argument("--max-open-positions", type=int, default=8)
    research.add_argument("--max-daily-orders", type=int, default=4)
    research.add_argument("--max-position-pct", type=float, default=0.18)
    research.add_argument("--max-order-pct", type=float, default=0.08)
    research.add_argument("--target-cash-pct", type=float, default=0.08)
    research.add_argument("--stop-loss-pct", type=float, default=0.08)
    research.add_argument("--slippage-bps", type=float, default=5.0)
    research.add_argument("--commission", type=float, default=0.0)
    research.add_argument("--max-drawdown-stop-pct", type=float, default=0.20)
    research.add_argument("--trailing-stop-pct", type=float, default=0.05)
    research.add_argument("--allow-position-adds", action="store_true", help="Allow buying more of symbols already held")
    research.add_argument("--buy-cooldown-days", type=int, default=3)

    risk_research = subparsers.add_parser("risk-research", help="Compare stop-loss and trailing-stop settings for one strategy")
    risk_research.add_argument("--symbols", nargs="+", default=POPULAR_STOCKS)
    risk_research.add_argument("--symbols-file")
    risk_research.add_argument("--data", choices=["demo", "alpaca"], default="alpaca")
    risk_research.add_argument("--days", type=int, default=1095)
    risk_research.add_argument("--feed", default="iex")
    risk_research.add_argument("--cash", type=float, default=100_000.0)
    risk_research.add_argument("--profile", choices=PROFILE_CHOICES, default="paper-operator")
    risk_research.add_argument("--strategy", choices=STRATEGY_CHOICES, default="ensemble")
    risk_research.add_argument("--short-window", type=int, default=5)
    risk_research.add_argument("--long-window", type=int, default=20)
    risk_research.add_argument("--min-confidence", type=float, default=0.65)
    risk_research.add_argument("--stop-loss-grid", default="0.05,0.08,0.10,0.12")
    risk_research.add_argument("--trailing-stop-grid", default="0.03,0.05,0.08,0.10")
    risk_research.add_argument("--max-open-positions", type=int, default=4)
    risk_research.add_argument("--max-daily-orders", type=int, default=2)
    risk_research.add_argument("--max-position-pct", type=float, default=0.25)
    risk_research.add_argument("--max-order-pct", type=float, default=0.10)
    risk_research.add_argument("--target-cash-pct", type=float, default=0.08)
    risk_research.add_argument("--slippage-bps", type=float, default=5.0)
    risk_research.add_argument("--commission", type=float, default=0.0)
    risk_research.add_argument("--max-drawdown-stop-pct", type=float, default=0.15)
    risk_research.add_argument("--buy-cooldown-days", type=int, default=3)
    risk_research.add_argument("--csv-out", default="risk_research_results.csv")
    risk_research.add_argument("--top", type=int, default=10)

    run = subparsers.add_parser("run", help="Run the bot")
    run.add_argument("--symbols", nargs="+", default=["AAPL", "MSFT"], help="Symbols to trade")
    run.add_argument("--symbols-file", help="Optional file of symbols, one per line or comma-separated")
    run.add_argument("--cash", type=float, default=10_000.0, help="Starting paper cash")
    run.add_argument("--csv", help="Optional CSV candle file")
    run.add_argument("--data", choices=["demo", "csv", "alpaca"], default=None, help="Market data source")
    run.add_argument("--days", type=int, default=180, help="Generated demo days when no CSV is provided")
    run.add_argument("--feed", default="iex", help="Alpaca market data feed, defaults to iex")
    run.add_argument("--short-window", type=int, default=10)
    run.add_argument("--long-window", type=int, default=30)
    run.add_argument("--min-confidence", type=float, default=0.55)
    run.add_argument("--strategy", choices=STRATEGY_CHOICES, default="ensemble")
    run.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
    run.add_argument("--max-open-positions", type=int, default=10)
    run.add_argument("--max-daily-orders", type=int, default=5)
    run.add_argument("--max-position-pct", type=float, default=0.25)
    run.add_argument("--max-order-pct", type=float, default=0.10)
    run.add_argument("--target-cash-pct", type=float, default=0.05)
    run.add_argument("--stop-loss-pct", type=float, default=0.08)
    run.add_argument("--slippage-bps", type=float, default=5.0)
    run.add_argument("--commission", type=float, default=0.0)
    run.add_argument("--max-drawdown-stop-pct", type=float, default=0.20)
    run.add_argument("--trailing-stop-pct", type=float, default=0.05)
    run.add_argument("--allow-position-adds", action="store_true", help="Allow buying more of symbols already held")
    run.add_argument("--buy-cooldown-days", type=int, default=3)

    alpaca = subparsers.add_parser("alpaca-once", help="Evaluate latest Alpaca signal and optionally submit one paper order")
    alpaca.add_argument("--symbols", nargs="+", default=["AAPL", "MSFT"], help="Symbols to evaluate")
    alpaca.add_argument("--symbols-file", help="Optional file of symbols, one per line or comma-separated")
    alpaca.add_argument("--cash", type=float, default=10_000.0, help="Fallback cash for config sizing")
    alpaca.add_argument("--days", type=int, default=180, help="Historical lookback days")
    alpaca.add_argument("--feed", default="iex", help="Alpaca market data feed, defaults to iex")
    alpaca.add_argument("--short-window", type=int, default=10)
    alpaca.add_argument("--long-window", type=int, default=30)
    alpaca.add_argument("--min-confidence", type=float, default=0.55)
    alpaca.add_argument("--strategy", choices=STRATEGY_CHOICES, default="ensemble")
    alpaca.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
    alpaca.add_argument("--max-open-positions", type=int, default=10)
    alpaca.add_argument("--max-daily-orders", type=int, default=5)
    alpaca.add_argument("--max-position-pct", type=float, default=0.25)
    alpaca.add_argument("--max-order-pct", type=float, default=0.10)
    alpaca.add_argument("--target-cash-pct", type=float, default=0.05)
    alpaca.add_argument("--stop-loss-pct", type=float, default=0.08)
    alpaca.add_argument("--slippage-bps", type=float, default=5.0)
    alpaca.add_argument("--commission", type=float, default=0.0)
    alpaca.add_argument("--max-drawdown-stop-pct", type=float, default=0.20)
    alpaca.add_argument("--trailing-stop-pct", type=float, default=0.05)
    alpaca.add_argument("--journal-dir", default="var")
    alpaca.add_argument("--allow-closed-submit", action="store_true", help="Allow queuing paper orders while market is closed")
    alpaca.add_argument(
        "--close-buffer-minutes",
        type=float,
        default=15.0,
        help="Refuse new buy submissions this many minutes before market close",
    )
    alpaca.add_argument(
        "--open-buffer-minutes",
        type=float,
        default=10.0,
        help="Refuse new buy submissions this many minutes after market open",
    )
    alpaca.add_argument("--allow-near-open", action="store_true", help="Allow new buy submissions inside the open buffer")
    alpaca.add_argument("--allow-near-close", action="store_true", help="Allow new buy submissions inside the close buffer")
    alpaca.add_argument("--sell-only", action="store_true", help="Disable new buy orders; exits remain allowed")
    alpaca.add_argument("--allow-position-adds", action="store_true", help="Allow buying more of symbols already held")
    alpaca.add_argument("--buy-cooldown-days", type=int, default=3)
    alpaca.add_argument("--submit", action="store_true", help="Actually submit generated orders to Alpaca paper")

    loop = subparsers.add_parser("alpaca-loop", help="Run Alpaca latest-signal checks repeatedly")
    loop.add_argument("--symbols", nargs="+", default=["AAPL", "MSFT"], help="Symbols to evaluate")
    loop.add_argument("--symbols-file", help="Optional file of symbols, one per line or comma-separated")
    loop.add_argument("--cash", type=float, default=10_000.0)
    loop.add_argument("--days", type=int, default=180)
    loop.add_argument("--feed", default="iex")
    loop.add_argument("--short-window", type=int, default=10)
    loop.add_argument("--long-window", type=int, default=30)
    loop.add_argument("--min-confidence", type=float, default=0.55)
    loop.add_argument("--strategy", choices=STRATEGY_CHOICES, default="ensemble")
    loop.add_argument("--profile", choices=PROFILE_CHOICES, default="custom")
    loop.add_argument("--max-open-positions", type=int, default=10)
    loop.add_argument("--max-daily-orders", type=int, default=5)
    loop.add_argument("--max-position-pct", type=float, default=0.25)
    loop.add_argument("--max-order-pct", type=float, default=0.10)
    loop.add_argument("--target-cash-pct", type=float, default=0.05)
    loop.add_argument("--stop-loss-pct", type=float, default=0.08)
    loop.add_argument("--slippage-bps", type=float, default=5.0)
    loop.add_argument("--commission", type=float, default=0.0)
    loop.add_argument("--max-drawdown-stop-pct", type=float, default=0.20)
    loop.add_argument("--trailing-stop-pct", type=float, default=0.05)
    loop.add_argument("--journal-dir", default="var")
    loop.add_argument("--allow-closed-submit", action="store_true", help="Allow queuing paper orders while market is closed")
    loop.add_argument(
        "--close-buffer-minutes",
        type=float,
        default=15.0,
        help="Refuse new buy submissions this many minutes before market close",
    )
    loop.add_argument(
        "--open-buffer-minutes",
        type=float,
        default=10.0,
        help="Refuse new buy submissions this many minutes after market open",
    )
    loop.add_argument("--allow-near-open", action="store_true", help="Allow new buy submissions inside the open buffer")
    loop.add_argument("--allow-near-close", action="store_true", help="Allow new buy submissions inside the close buffer")
    loop.add_argument("--sell-only", action="store_true", help="Disable new buy orders; exits remain allowed")
    loop.add_argument("--allow-position-adds", action="store_true", help="Allow buying more of symbols already held")
    loop.add_argument("--buy-cooldown-days", type=int, default=3)
    loop.add_argument("--interval-minutes", type=float, default=15.0)
    loop.add_argument("--cycles", type=int, default=1, help="Number of cycles to run; use 0 for endless")
    loop.add_argument("--submit", action="store_true", help="Actually submit generated orders to Alpaca paper")

    risk = subparsers.add_parser("alpaca-risk", help="Run a fast risk/exit check without scanning new buy signals")
    add_live_arguments(risk)

    session = subparsers.add_parser("alpaca-session", help="Run frequent risk checks and slower signal checks")
    add_live_arguments(session)
    session.add_argument("--risk-interval-minutes", type=float, default=5.0)
    session.add_argument("--signal-interval-minutes", type=float, default=30.0)
    session.add_argument("--protective-stop-sync-minutes", type=float, default=15.0)
    session.add_argument("--no-protective-stops", action="store_true", help="Do not create or raise broker-side stops during the session")
    session.add_argument("--cycles", type=int, default=12, help="Number of risk cycles to run; use 0 for endless")
    session.add_argument("--until-close", action="store_true", help="Run until Alpaca reports that the market is closed")
    session.add_argument("--wait-for-open", action="store_true", help="Wait for market open before starting the session")
    session.add_argument("--max-consecutive-errors", type=int, default=5, help="Retry transient session failures before exiting")
    session.add_argument("--shadow-strategy", choices=STRATEGY_CHOICES, help="Record a second strategy without submitting its orders")
    session.add_argument("--shadow-journal-dir", default="var\\shadow")

    protect = subparsers.add_parser("alpaca-protect", help="Create broker-side protective stop orders for held positions")
    protect.add_argument("--journal-dir", default="var")
    protect.add_argument("--stop-loss-pct", type=float, default=0.08)
    protect.add_argument("--trailing-stop-pct", type=float, default=0.05)
    protect.add_argument("--submit", action="store_true", help="Actually submit missing protective stops to Alpaca paper")

    smoke = subparsers.add_parser(
        "alpaca-exec-smoke",
        help="Run a tiny paper-only buy/protect/cancel/sell execution reliability check",
    )
    smoke.add_argument("--symbol", default="MSFT", help="Liquid symbol to use for the round trip")
    smoke.add_argument("--quantity", type=int, default=1, help="Share quantity for the test")
    smoke.add_argument("--max-notional", type=float, default=1_000.0, help="Refuse if estimated order value is above this")
    smoke.add_argument("--stop-pct", type=float, default=0.03, help="Temporary protective stop distance")
    smoke.add_argument("--feed", default="iex")
    smoke.add_argument("--journal-dir", default="var\\execution_smoke")
    smoke.add_argument("--submit", action="store_true", help="Actually submit the paper round trip")

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["demo"])
    _apply_profile(args, _provided_options(raw_args))

    if args.command == "doctor":
        _doctor()
        return 0

    if args.command == "clock":
        _clock()
        return 0

    if args.command == "daily-report":
        _daily_report(args)
        return 0

    if args.command == "strategy-report":
        _strategy_report(args)
        return 0

    if args.command == "close-audit":
        _close_audit(args)
        return 0

    if args.command == "promotion-report":
        _promotion_report(args)
        return 0

    if args.command == "risk-promotion-report":
        _risk_promotion_report(args)
        return 0

    if args.command == "status":
        _status(args)
        return 0

    if args.command == "reset-state":
        _reset_state(args)
        return 0

    if args.command == "research":
        _research(args)
        return 0

    if args.command == "risk-research":
        _risk_research(args)
        return 0

    if args.command == "demo":
        config = BotConfig(symbols=["AAPL", "MSFT", "NVDA"], starting_cash=10_000.0)
        provider = DemoMarketDataProvider(days=220)
    elif args.command == "run":
        symbols = _resolve_symbols(args.symbols, args.symbols_file)
        config = BotConfig(
            symbols=symbols,
            starting_cash=args.cash,
            short_window=args.short_window,
            long_window=args.long_window,
            min_confidence=args.min_confidence,
            max_open_positions=args.max_open_positions,
            max_daily_orders=args.max_daily_orders,
            max_position_pct=args.max_position_pct,
            max_order_pct=args.max_order_pct,
            target_cash_pct=args.target_cash_pct,
            stop_loss_pct=args.stop_loss_pct,
            slippage_bps=args.slippage_bps,
            commission_per_trade=args.commission,
            max_drawdown_stop_pct=args.max_drawdown_stop_pct,
            trailing_stop_pct=args.trailing_stop_pct,
            allow_position_adds=args.allow_position_adds,
            buy_cooldown_days=args.buy_cooldown_days,
        )
        data_source = args.data or ("csv" if args.csv else "demo")
        if data_source == "alpaca":
            provider = AlpacaMarketDataProvider(days=args.days, feed=args.feed)
        elif data_source == "csv":
            if not args.csv:
                parser.error("--data csv requires --csv")
            provider = CsvMarketDataProvider(args.csv)
        else:
            provider = DemoMarketDataProvider(days=args.days)
    elif args.command == "alpaca-once":
        result = _run_alpaca_once(args)
        _print_live_result(result, submitted=args.submit)
        return 0
    elif args.command == "alpaca-risk":
        result = _run_alpaca_risk_once(args)
        _print_live_result(result, submitted=args.submit)
        return 0
    elif args.command == "alpaca-session":
        _run_alpaca_session(args)
        return 0
    elif args.command == "alpaca-protect":
        _alpaca_protect(args)
        return 0
    elif args.command == "alpaca-exec-smoke":
        _alpaca_exec_smoke(args)
        return 0
    else:
        cycles = 0
        while args.cycles == 0 or cycles < args.cycles:
            cycles += 1
            print(f"Cycle {cycles}")
            result = _run_alpaca_once(args)
            _print_live_result(result, submitted=args.submit)
            if args.cycles != 0 and cycles >= args.cycles:
                break
            time.sleep(max(1.0, args.interval_minutes * 60))
        return 0

    engine = TradingEngine(
        config=config,
        data_provider=provider,
        strategy=_build_strategy(getattr(args, "strategy", "ma"), config),
        advisor=HeuristicAdvisor(),
    )
    result = engine.run()
    _print_result(config, result)
    return 0


def _run_alpaca_once(args):
    symbols = _resolve_symbols(args.symbols, args.symbols_file)
    config = _live_config(args, symbols)
    broker = AlpacaPaperBroker()
    journal = TradeJournal(args.journal_dir)
    if not getattr(args, "shadow_mode", False):
        _sync_alpaca_protective_fills(journal, broker)
    reconciliation = reconcile_positions(journal, broker)
    clock = None
    if getattr(args, "submit", False) and not getattr(args, "allow_closed_submit", False):
        clock = broker.market_clock()
        if not clock.is_open:
            raise RuntimeError(
                "Market is closed. Re-run after "
                f"{format_local(clock.next_open)} Lithuania time, or pass --allow-closed-submit to queue the paper order."
            )
    if (
        getattr(args, "submit", False)
        and not getattr(args, "sell_only", False)
        and not getattr(args, "allow_near_open", False)
    ):
        clock = clock or broker.market_clock()
        if clock.is_open:
            minutes_since_open = _minutes_since_regular_open(clock.timestamp)
            open_buffer = getattr(args, "open_buffer_minutes", 10.0)
            if 0 <= minutes_since_open < open_buffer:
                raise RuntimeError(
                    "Too close to market open for fresh buy submissions "
                    f"({minutes_since_open:.1f} minutes after open, buffer is {open_buffer:.1f}). "
                    "Use --sell-only to keep exits active, or --allow-near-open if you deliberately want this."
                )
    if (
        getattr(args, "submit", False)
        and not getattr(args, "sell_only", False)
        and not getattr(args, "allow_near_close", False)
    ):
        clock = clock or broker.market_clock()
        if clock.is_open:
            minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
            close_buffer = getattr(args, "close_buffer_minutes", 15.0)
            if minutes_to_close <= close_buffer:
                raise RuntimeError(
                    "Too close to market close for fresh buy submissions "
                    f"({minutes_to_close:.1f} minutes left, buffer is {close_buffer:.1f}). "
                    "Use --sell-only to keep exits active, or --allow-near-close if you deliberately want this."
                )
    if getattr(args, "submit", False) and not reconciliation.ok:
        raise RuntimeError(
            "Bot/Alpaca position reconciliation failed; refusing to submit. "
            + " | ".join(reconciliation.messages)
        )

    clock = clock or broker.market_clock()
    incomplete_date = clock.timestamp.date() if clock.is_open else None
    lookback_days = max(args.days, 400) if args.strategy == "relative-strength" else args.days
    engine = LiveTradingEngine(
        config=config,
        data_provider=AlpacaMarketDataProvider(
            days=lookback_days,
            feed=args.feed,
            exclude_date=incomplete_date,
        ),
        strategy=_build_strategy(args.strategy, config),
        advisor=HeuristicAdvisor(),
        broker=broker,
        journal=journal,
    )
    return engine.run_once(submit_orders=args.submit)


def _run_alpaca_risk_once(args):
    symbols = _resolve_symbols(args.symbols, args.symbols_file)
    config = _live_config(args, symbols, allow_new_buys=False)
    broker = AlpacaPaperBroker()
    journal = TradeJournal(args.journal_dir)
    _sync_alpaca_protective_fills(journal, broker)
    reconciliation = reconcile_positions(journal, broker)
    if getattr(args, "submit", False) and not getattr(args, "allow_closed_submit", False):
        clock = broker.market_clock()
        if not clock.is_open:
            raise RuntimeError(
                "Market is closed. Re-run after "
                f"{format_local(clock.next_open)} Lithuania time, or pass --allow-closed-submit to queue the paper order."
            )
    if getattr(args, "submit", False) and not reconciliation.ok:
        raise RuntimeError(
            "Bot/Alpaca position reconciliation failed; refusing to submit. "
            + " | ".join(reconciliation.messages)
        )

    engine = LiveTradingEngine(
        config=config,
        data_provider=AlpacaMarketDataProvider(days=args.days, feed=args.feed),
        strategy=_build_strategy(args.strategy, config),
        advisor=HeuristicAdvisor(),
        broker=broker,
        journal=journal,
    )
    return engine.run_risk_once(submit_orders=args.submit)


def _run_alpaca_session(args) -> None:
    cycles = 0
    next_signal_at = 0.0
    next_protect_at = 0.0
    while args.until_close or args.cycles == 0 or cycles < args.cycles:
        clock = None
        if _session_needs_clock(args):
            clock = _session_retry(args, "Market clock", lambda: AlpacaPaperBroker().market_clock())
            if not clock.is_open:
                if getattr(args, "wait_for_open", False):
                    wait_seconds = _session_wait_for_open_seconds(args, clock)
                    print(
                        f"Market is closed. Waiting until "
                        f"{format_local(clock.next_open)} Lithuania time"
                        f"{' plus open buffer' if _fresh_buy_open_buffer_enabled(args) else ''}."
                    )
                    time.sleep(wait_seconds)
                    continue
                print(f"Market is closed. Next open: {format_local(clock.next_open)} Lithuania time.")
                break
        cycles += 1
        print(f"Risk cycle {cycles}")
        risk_result = _session_retry(args, "Risk cycle", lambda: _run_alpaca_risk_once(args))
        _print_live_result(risk_result, submitted=args.submit)

        now = time.monotonic()
        if args.submit and not getattr(args, "no_protective_stops", False) and now >= next_protect_at:
            print()
            print("Protective stop sync")
            _session_retry(args, "Protective stop sync", lambda: _alpaca_protect(args))
            next_protect_at = now + max(1.0, args.protective_stop_sync_minutes * 60)

        if now >= next_signal_at:
            if _should_defer_signal_cycle_for_open_buffer(args, clock):
                wait_seconds = _open_buffer_remaining_seconds(args, clock)
                print()
                print(f"Signal cycle deferred until open buffer ends in {wait_seconds / 60:.1f} minutes")
                next_signal_at = now + min(max(1.0, args.risk_interval_minutes * 60), wait_seconds)
            else:
                print()
                print("Signal cycle")
                signal_result = _session_retry(args, "Signal cycle", lambda: _run_alpaca_once(args))
                _print_live_result(signal_result, submitted=args.submit)
                if getattr(args, "shadow_strategy", None) and args.shadow_strategy != args.strategy:
                    print()
                    print(f"Shadow signal cycle ({args.shadow_strategy})")
                    shadow_args = _shadow_session_args(args)
                    shadow_result = _session_retry(
                        args,
                        "Shadow signal cycle",
                        lambda: _run_alpaca_once(shadow_args),
                    )
                    _print_live_result(shadow_result, submitted=False)
                next_signal_at = now + max(1.0, args.signal_interval_minutes * 60)

        if not args.until_close and args.cycles != 0 and cycles >= args.cycles:
            break
        time.sleep(_session_sleep_seconds(args, clock))


def _session_retry(args, label: str, operation):
    attempts = max(1, int(getattr(args, "max_consecutive_errors", 5)))
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except RuntimeError as exc:
            if attempt >= attempts:
                raise
            delay = min(60.0, 5.0 * attempt)
            print(f"{label} failed ({attempt}/{attempts}): {exc}")
            print(f"Retrying in {delay:.0f} seconds.")
            time.sleep(delay)


def _shadow_session_args(args):
    values = vars(args).copy()
    values.update(
        {
            "strategy": args.shadow_strategy,
            "journal_dir": args.shadow_journal_dir,
            "submit": False,
            "sell_only": False,
            "allow_closed_submit": False,
            "shadow_mode": True,
        }
    )
    return argparse.Namespace(**values)


def _session_sleep_seconds(args, clock=None) -> float:
    interval_seconds = max(1.0, args.risk_interval_minutes * 60)
    if not getattr(args, "until_close", False) or clock is None:
        return interval_seconds
    seconds_to_close = max(1.0, (clock.next_close - clock.timestamp).total_seconds())
    return min(interval_seconds, seconds_to_close)


def _session_needs_clock(args) -> bool:
    return (
        getattr(args, "until_close", False)
        or getattr(args, "wait_for_open", False)
        or _fresh_buy_open_buffer_enabled(args)
    )


def _session_wait_for_open_seconds(args, clock) -> float:
    seconds = max(1.0, (clock.next_open - clock.timestamp).total_seconds())
    if _fresh_buy_open_buffer_enabled(args):
        seconds += max(0.0, getattr(args, "open_buffer_minutes", 10.0) * 60)
    return seconds


def _fresh_buy_open_buffer_enabled(args) -> bool:
    return (
        getattr(args, "submit", False)
        and not getattr(args, "sell_only", False)
        and not getattr(args, "allow_near_open", False)
    )


def _should_defer_signal_cycle_for_open_buffer(args, clock) -> bool:
    if not clock or not getattr(clock, "is_open", False) or not _fresh_buy_open_buffer_enabled(args):
        return False
    return _open_buffer_remaining_seconds(args, clock) > 0


def _open_buffer_remaining_seconds(args, clock) -> float:
    minutes_since_open = _minutes_since_regular_open(clock.timestamp)
    open_buffer = getattr(args, "open_buffer_minutes", 10.0)
    remaining_minutes = open_buffer - minutes_since_open
    return max(0.0, remaining_minutes * 60)


def _alpaca_protect(args) -> None:
    broker = AlpacaPaperBroker()
    journal = TradeJournal(args.journal_dir)
    _sync_alpaca_protective_fills(journal, broker)
    reconciliation = reconcile_positions(journal, broker)
    if args.submit and not reconciliation.ok:
        raise RuntimeError(
            "Bot/Alpaca position reconciliation failed; refusing to submit protective stops. "
            + " | ".join(reconciliation.messages)
        )

    remote_positions = broker._client.get_all_positions()
    _sync_position_highs_from_positions(journal, remote_positions)
    state = journal._read_state()
    risk_rows = _position_risk_rows(
        remote_positions,
        state,
        stop_loss_pct=args.stop_loss_pct,
        trailing_stop_pct=args.trailing_stop_pct,
    )
    open_sell_orders = _open_sell_orders_by_symbol(broker.open_orders())

    print("Broker-side protective stops")
    if not risk_rows:
        print("No held positions.")
        return

    for row in risk_rows:
        symbol = str(row["symbol"])
        position = broker.positions.get(symbol)
        if not position or position.quantity <= 0:
            continue
        stop_price = float(row["active_stop"])
        existing_order = open_sell_orders.get(symbol)
        if existing_order:
            existing_stop = float(getattr(existing_order, "stop_price", 0.0) or 0.0)
            if stop_price > existing_stop + 0.01:
                print(f"{symbol:6} RAISE STOP {position.quantity} ${existing_stop:.2f} -> ${stop_price:.2f}")
                if args.submit:
                    replacement = broker.replace_stop_order(existing_order.id, stop_price, position.quantity)
                    client_order_id = str(getattr(replacement, "client_order_id", "") or existing_order.client_order_id)
                    journal.mark_protective_order(symbol, client_order_id, stop_price, position.quantity)
            else:
                print(f"{symbol:6} SKIP existing open sell order @ ${existing_stop:.2f}")
                journal.mark_protective_order(symbol, str(existing_order.client_order_id), existing_stop, position.quantity)
            continue
        client_order_id = f"bot-protect-{symbol.lower()}-{broker.trading_date().isoformat()}"
        print(f"{symbol:6} STOP SELL {position.quantity} @ ${stop_price:.2f}")
        if args.submit:
            broker.submit_protective_stop(symbol, position.quantity, stop_price, client_order_id)
            journal.mark_protective_order(symbol, client_order_id, stop_price, position.quantity)


def _alpaca_exec_smoke(args) -> None:
    symbol = args.symbol.upper()
    broker = AlpacaPaperBroker()
    clock = broker.market_clock()
    journal = TradeJournal(args.journal_dir)
    price = _latest_alpaca_price(symbol, args.feed)
    notional = price * args.quantity

    _validate_exec_smoke_args(args, symbol, price, broker)

    print("Alpaca paper execution smoke test")
    print(f"Symbol:      {symbol}")
    print(f"Quantity:    {args.quantity}")
    print(f"Est. price:  ${price:,.2f}")
    print(f"Est. value:  ${notional:,.2f}")
    print(f"Market open: {clock.is_open}")
    print(f"Mode:        {'submit paper round trip' if args.submit else 'dry run'}")

    if not args.submit:
        print("Dry run only. Add --submit to place the 1-share paper round trip.")
        return
    if not clock.is_open:
        raise RuntimeError(f"Market is closed. Re-run after {format_local(clock.next_open)} Lithuania time.")

    trade_date = broker.trading_date()
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    buy_order = Order(
        symbol=symbol,
        side=Side.BUY,
        quantity=args.quantity,
        price=price,
        reason="execution smoke test buy",
        client_order_id=f"bot-smoke-{run_id}-{symbol.lower()}-buy",
    )
    buy_fill = broker.submit(buy_order, trade_date)
    if not buy_fill:
        raise RuntimeError("Smoke buy did not return a fill.")
    journal.record_fill(buy_fill, mode="alpaca-exec-smoke")
    journal.mark_submitted(TradeJournal.order_key(buy_order, buy_fill.date))
    print(f"BUY filled:  {buy_fill.quantity} {symbol} @ ${buy_fill.price:,.2f}")

    stop_price = round(buy_fill.price * (1 - args.stop_pct), 2)
    stop_id = f"bot-smoke-{run_id}-{symbol.lower()}-stop"
    broker.submit_protective_stop(symbol, buy_fill.quantity, stop_price, stop_id)
    journal.mark_protective_order(symbol, stop_id, stop_price, buy_fill.quantity)
    print(f"STOP set:    {buy_fill.quantity} {symbol} @ ${stop_price:,.2f}")

    canceled = broker.cancel_open_sell_orders(symbol)
    journal.clear_protective_order(symbol)
    print(f"STOP cancel: {canceled} open sell order(s)")

    sell_order = Order(
        symbol=symbol,
        side=Side.SELL,
        quantity=buy_fill.quantity,
        price=buy_fill.price,
        reason="execution smoke test sell",
        client_order_id=f"bot-smoke-{run_id}-{symbol.lower()}-sell",
    )
    sell_fill = broker.submit(sell_order, trade_date)
    if not sell_fill:
        raise RuntimeError("Smoke sell did not return a fill.")
    journal.record_fill(sell_fill, mode="alpaca-exec-smoke")
    journal.mark_submitted(TradeJournal.order_key(sell_order, sell_fill.date))
    print(f"SELL filled: {sell_fill.quantity} {symbol} @ ${sell_fill.price:,.2f}")

    broker.refresh()
    remaining = broker.positions.get(symbol)
    if remaining and remaining.quantity > 0:
        raise RuntimeError(f"Smoke test left an unexpected {symbol} position of {remaining.quantity} shares.")
    print("Result:      ok, no smoke-test position remains")


def _validate_exec_smoke_args(args, symbol: str, price: float, broker: AlpacaPaperBroker) -> None:
    if args.quantity <= 0:
        raise ValueError("--quantity must be positive")
    if args.max_notional <= 0:
        raise ValueError("--max-notional must be positive")
    if not 0 < args.stop_pct < 1:
        raise ValueError("--stop-pct must be between 0 and 1")
    if price <= 0:
        raise RuntimeError(f"Could not estimate a positive price for {symbol}.")
    notional = price * args.quantity
    if notional > args.max_notional:
        raise RuntimeError(f"Estimated notional ${notional:,.2f} exceeds --max-notional ${args.max_notional:,.2f}.")
    if symbol in broker.positions:
        raise RuntimeError(f"{symbol} is already held. Use an unheld liquid symbol for the smoke test.")


def _latest_alpaca_price(symbol: str, feed: str) -> float:
    data = AlpacaMarketDataProvider(days=10, feed=feed).load([symbol])
    rows = data.get(symbol.upper(), [])
    if not rows:
        raise RuntimeError(f"No recent Alpaca data returned for {symbol}.")
    return rows[-1].close


def _open_sell_order_symbols(open_orders) -> set[str]:
    return set(_open_sell_orders_by_symbol(open_orders))


def _active_open_orders(open_orders) -> list[object]:
    return [order for order in open_orders if _active_order_status(order)]


def _open_sell_orders_by_symbol(open_orders) -> dict[str, object]:
    orders: dict[str, object] = {}
    for order in open_orders:
        side = getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))
        if str(side).lower() == Side.SELL.value:
            symbol = str(order.symbol).upper()
            if not _active_order_status(order):
                continue
            orders[symbol] = order
    return orders


def _active_order_status(order) -> bool:
    status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))).lower()
    return status not in {"pending_replace", "pending_cancel", "replaced", "canceled", "expired"}


def _sync_alpaca_protective_fills(journal: TradeJournal, broker: AlpacaPaperBroker) -> int:
    synced = 0
    if not hasattr(broker, "closed_orders"):
        return synced
    state = journal._read_state()
    protective_orders = state.get("protective_orders", {})
    protective_order_ids = {
        str(details.get("client_order_id", ""))
        for details in protective_orders.values()
        if isinstance(details, dict) and details.get("client_order_id")
    }
    for remote_order in broker.closed_orders():
        client_order_id = str(getattr(remote_order, "client_order_id", "") or "")
        if not _is_known_protective_order(remote_order, client_order_id, protective_order_ids):
            continue
        if journal.has_synced_order(client_order_id):
            continue
        status = getattr(getattr(remote_order, "status", ""), "value", getattr(remote_order, "status", ""))
        if str(status).lower() != "filled":
            continue
        fill = _fill_from_remote_protective_order(remote_order)
        if not fill:
            continue
        journal.record_fill(fill, mode="alpaca-paper-sync")
        journal.mark_submitted(TradeJournal.order_key(fill_to_order(fill), fill.date))
        journal.mark_synced_order(client_order_id)
        journal.clear_protective_order(fill.symbol)
        synced += 1
    return synced


def _is_known_protective_order(remote_order, client_order_id: str, protective_order_ids: set[str]) -> bool:
    return client_order_id.startswith("bot-protect-") or client_order_id in protective_order_ids


def _fill_from_remote_protective_order(remote_order):
    quantity = int(float(getattr(remote_order, "filled_qty", 0) or 0))
    price = float(getattr(remote_order, "filled_avg_price", 0) or 0)
    if quantity <= 0 or price <= 0:
        return None
    filled_at = getattr(remote_order, "filled_at", None)
    fill_date = filled_at.astimezone(MARKET_TZ).date() if filled_at else date.today()
    return Fill(
        date=fill_date,
        symbol=str(remote_order.symbol).upper(),
        side=Side.SELL,
        quantity=quantity,
        price=price,
        value=quantity * price,
        reason=f"synced Alpaca protective stop: {getattr(remote_order, 'client_order_id', '')}",
    )


def fill_to_order(fill: Fill):
    return Order(fill.symbol, fill.side, fill.quantity, fill.price, fill.reason)


def _live_config(args, symbols: list[str], allow_new_buys: bool | None = None) -> BotConfig:
    if allow_new_buys is None:
        allow_new_buys = not getattr(args, "sell_only", False)
    return BotConfig(
        symbols=symbols,
        starting_cash=args.cash,
        short_window=args.short_window,
        long_window=args.long_window,
        min_confidence=args.min_confidence,
        max_open_positions=args.max_open_positions,
        max_daily_orders=args.max_daily_orders,
        max_position_pct=args.max_position_pct,
        max_order_pct=args.max_order_pct,
        target_cash_pct=args.target_cash_pct,
        stop_loss_pct=args.stop_loss_pct,
        slippage_bps=args.slippage_bps,
        commission_per_trade=args.commission,
        max_drawdown_stop_pct=args.max_drawdown_stop_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        allow_new_buys=allow_new_buys,
        allow_position_adds=getattr(args, "allow_position_adds", False),
        buy_cooldown_days=getattr(args, "buy_cooldown_days", 3),
    )


def _research(args) -> None:
    symbols = _resolve_symbols(args.symbols, args.symbols_file)
    provider = (
        AlpacaMarketDataProvider(days=args.days, feed=args.feed)
        if args.data == "alpaca"
        else DemoMarketDataProvider(days=args.days)
    )
    print(f"Loading {args.data} data for {len(symbols)} symbols...")
    data = provider.load(symbols)
    loaded = [symbol for symbol, rows in data.items() if rows]
    if not loaded:
        raise RuntimeError("No market data was loaded for the requested symbols.")

    config = BotConfig(
        symbols=loaded,
        starting_cash=args.cash,
        max_open_positions=args.max_open_positions,
        max_daily_orders=args.max_daily_orders,
        max_position_pct=args.max_position_pct,
        max_order_pct=args.max_order_pct,
        target_cash_pct=args.target_cash_pct,
        stop_loss_pct=args.stop_loss_pct,
        slippage_bps=args.slippage_bps,
        commission_per_trade=args.commission,
        max_drawdown_stop_pct=args.max_drawdown_stop_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        allow_position_adds=args.allow_position_adds,
        buy_cooldown_days=args.buy_cooldown_days,
    )
    specs = build_expanded_strategy_specs() if args.expanded else build_strategy_specs()
    if args.strategy_filter:
        requested = {name.strip() for name in args.strategy_filter.split(",") if name.strip()}
        specs = [spec for spec in specs if spec.name in requested]
        found = {spec.name for spec in specs}
        missing = sorted(requested - found)
        if missing:
            raise ValueError("Unknown strategy names: " + ", ".join(missing))
    rows = evaluate_specs(data, loaded, config, specs=specs, train_pct=args.train_pct)
    if args.csv_out:
        _write_research_csv(args.csv_out, rows)
    _print_research(rows[: args.top], loaded)
    if args.walk_forward:
        walk_rows = evaluate_walk_forward(data, loaded, config, specs=specs, folds=args.folds)
        if args.walk_forward_csv_out:
            _write_walk_forward_csv(args.walk_forward_csv_out, walk_rows)
        _print_walk_forward(walk_rows[: args.top])


def _risk_research(args) -> None:
    symbols = _resolve_symbols(args.symbols, args.symbols_file)
    provider = (
        AlpacaMarketDataProvider(days=args.days, feed=args.feed)
        if args.data == "alpaca"
        else DemoMarketDataProvider(days=args.days)
    )
    print(f"Loading {args.data} data for {len(symbols)} symbols...")
    data = provider.load(symbols)
    loaded = [symbol for symbol, rows in data.items() if rows]
    if not loaded:
        raise RuntimeError("No market data was loaded for the requested symbols.")

    stop_values = _parse_float_grid(args.stop_loss_grid, "--stop-loss-grid")
    trailing_values = _parse_float_grid(args.trailing_stop_grid, "--trailing-stop-grid")
    rows = []
    for stop_loss_pct in stop_values:
        for trailing_stop_pct in trailing_values:
            config = BotConfig(
                symbols=loaded,
                starting_cash=args.cash,
                short_window=args.short_window,
                long_window=args.long_window,
                min_confidence=args.min_confidence,
                max_open_positions=args.max_open_positions,
                max_daily_orders=args.max_daily_orders,
                max_position_pct=args.max_position_pct,
                max_order_pct=args.max_order_pct,
                target_cash_pct=args.target_cash_pct,
                stop_loss_pct=stop_loss_pct,
                trailing_stop_pct=trailing_stop_pct,
                slippage_bps=args.slippage_bps,
                commission_per_trade=args.commission,
                max_drawdown_stop_pct=args.max_drawdown_stop_pct,
                buy_cooldown_days=args.buy_cooldown_days,
            )
            engine = TradingEngine(
                config=config,
                data_provider=StaticMarketDataProvider(data),
                strategy=_build_strategy(args.strategy, config),
                advisor=HeuristicAdvisor(),
            )
            result = engine.run()
            rows.append(
                {
                    "stop_loss_pct": stop_loss_pct,
                    "trailing_stop_pct": trailing_stop_pct,
                    "return_pct": result.return_pct,
                    "max_drawdown_pct": _max_drawdown_from_snapshots(result.snapshots),
                    "trades": len(result.fills),
                    "score": _risk_research_score(result.return_pct, _max_drawdown_from_snapshots(result.snapshots), len(result.fills)),
                }
            )
    rows.sort(key=lambda row: row["score"], reverse=True)
    _write_risk_research_csv(args.csv_out, rows)
    _print_risk_research(rows[: args.top])


def _parse_float_grid(raw: str, option_name: str) -> list[float]:
    values: list[float] = []
    for part in raw.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        value = float(stripped)
        if not 0 < value < 1:
            raise ValueError(f"{option_name} values must be between 0 and 1")
        values.append(value)
    if not values:
        raise ValueError(f"{option_name} must contain at least one value")
    return values


def _risk_research_score(return_pct: float, max_drawdown_pct: float, trades: int) -> float:
    trade_penalty = min(3.0, trades * 0.01)
    return return_pct - abs(max_drawdown_pct) * 0.75 - trade_penalty


def _max_drawdown_from_snapshots(snapshots) -> float:
    peak = 0.0
    worst = 0.0
    for snapshot in snapshots:
        peak = max(peak, snapshot.equity)
        if peak:
            worst = min(worst, (snapshot.equity - peak) / peak * 100)
    return worst


def _resolve_symbols(symbols: list[str], symbols_file: str | None) -> list[str]:
    resolved = list(symbols)
    if symbols_file:
        raw = Path(symbols_file).read_text(encoding="utf-8")
        from_file = [part.strip() for part in raw.replace(",", "\n").splitlines()]
        resolved = [symbol for symbol in from_file if symbol and not symbol.startswith("#")]
    return sorted({symbol.upper() for symbol in resolved})


def _provided_options(raw_args: list[str]) -> set[str]:
    provided: set[str] = set()
    for value in raw_args:
        if value.startswith("--"):
            provided.add(value.split("=", 1)[0])
    return provided


def _set_profile_value(args, provided: set[str], option: str, attribute: str, value) -> None:
    if option not in provided:
        setattr(args, attribute, value)


def _apply_profile(args, provided: set[str]) -> None:
    profile = getattr(args, "profile", "custom")
    if profile == "balanced":
        _set_profile_value(args, provided, "--strategy", "strategy", "ensemble")
        _set_profile_value(args, provided, "--short-window", "short_window", 10)
        _set_profile_value(args, provided, "--long-window", "long_window", 30)
        _set_profile_value(args, provided, "--min-confidence", "min_confidence", 0.62)
        _set_profile_value(args, provided, "--max-open-positions", "max_open_positions", 5)
        _set_profile_value(args, provided, "--max-daily-orders", "max_daily_orders", 2)
        _set_profile_value(args, provided, "--max-position-pct", "max_position_pct", 0.20)
        _set_profile_value(args, provided, "--max-order-pct", "max_order_pct", 0.10)
        _set_profile_value(args, provided, "--target-cash-pct", "target_cash_pct", 0.05)
        _set_profile_value(args, provided, "--slippage-bps", "slippage_bps", 5.0)
        _set_profile_value(args, provided, "--max-drawdown-stop-pct", "max_drawdown_stop_pct", 0.18)
    elif profile == "aggressive-research":
        _set_profile_value(args, provided, "--strategy", "strategy", "ensemble")
        _set_profile_value(args, provided, "--short-window", "short_window", 5)
        _set_profile_value(args, provided, "--long-window", "long_window", 20)
        _set_profile_value(args, provided, "--min-confidence", "min_confidence", 0.62)
        _set_profile_value(args, provided, "--max-open-positions", "max_open_positions", 3)
        _set_profile_value(args, provided, "--max-daily-orders", "max_daily_orders", 1)
        _set_profile_value(args, provided, "--max-position-pct", "max_position_pct", 0.30)
        _set_profile_value(args, provided, "--max-order-pct", "max_order_pct", 0.15)
        _set_profile_value(args, provided, "--target-cash-pct", "target_cash_pct", 0.05)
        _set_profile_value(args, provided, "--slippage-bps", "slippage_bps", 5.0)
        _set_profile_value(args, provided, "--max-drawdown-stop-pct", "max_drawdown_stop_pct", 0.20)
    elif profile == "paper-operator":
        _set_profile_value(args, provided, "--strategy", "strategy", "ensemble")
        _set_profile_value(args, provided, "--short-window", "short_window", 5)
        _set_profile_value(args, provided, "--long-window", "long_window", 20)
        _set_profile_value(args, provided, "--min-confidence", "min_confidence", 0.65)
        _set_profile_value(args, provided, "--max-open-positions", "max_open_positions", 4)
        _set_profile_value(args, provided, "--max-daily-orders", "max_daily_orders", 2)
        _set_profile_value(args, provided, "--max-position-pct", "max_position_pct", 0.25)
        _set_profile_value(args, provided, "--max-order-pct", "max_order_pct", 0.10)
        _set_profile_value(args, provided, "--target-cash-pct", "target_cash_pct", 0.08)
        _set_profile_value(args, provided, "--stop-loss-pct", "stop_loss_pct", 0.10)
        _set_profile_value(args, provided, "--trailing-stop-pct", "trailing_stop_pct", 0.03)
        _set_profile_value(args, provided, "--slippage-bps", "slippage_bps", 5.0)
        _set_profile_value(args, provided, "--max-drawdown-stop-pct", "max_drawdown_stop_pct", 0.15)


def _doctor() -> None:
    from .alpaca_integration import _load_local_env
    import os

    _load_local_env()
    print("Trading bot setup")
    print("Python imports: ok")
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER", "ALPACA_BASE_URL"):
        value = os.getenv(key, "")
        status = "set" if value else "missing"
        print(f"{key}: {status}, length={len(value)}")


def _clock() -> None:
    broker = AlpacaPaperBroker()
    clock = broker.market_clock()
    print("Market clock")
    print(f"Lithuania now: {format_local(now_local())}")
    print(f"Alpaca time:   {format_market(clock.timestamp)}")
    print(f"Market open:   {clock.is_open}")
    print(f"Next open:     {format_local(clock.next_open)} Lithuania / {format_market(clock.next_open)}")
    print(f"Next close:    {format_local(clock.next_close)} Lithuania / {format_market(clock.next_close)}")


def _status(args) -> None:
    broker = AlpacaPaperBroker()
    clock = broker.market_clock()
    account = broker._client.get_account()
    positions = broker._client.get_all_positions()
    open_orders = _active_open_orders(broker._client.get_orders())
    journal = TradeJournal(args.journal_dir)
    _sync_alpaca_protective_fills(journal, broker)
    reconciliation = reconcile_positions(journal, broker)

    print("Alpaca paper status")
    print(f"Lithuania now: {format_local(now_local())}")
    print(f"Market open:   {clock.is_open}")
    print(f"Next open:     {format_local(clock.next_open)}")
    print(f"Next close:    {format_local(clock.next_close)}")
    print(f"Equity:        ${float(account.equity):,.2f}")
    print(f"Cash:          ${float(account.cash):,.2f}")
    print(f"Buying power:  ${float(account.buying_power):,.2f}")
    print()
    print("Positions")
    if not positions:
        print("none")
    for position in positions:
        print(
            f"{position.symbol:6} qty={position.qty:>8} avg=${float(position.avg_entry_price):,.2f} "
            f"value=${float(position.market_value):,.2f} pl=${float(position.unrealized_pl):,.2f} "
            f"pl%={float(position.unrealized_plpc) * 100:.2f}%"
        )
    print()
    print("Position risk")
    risk_rows = _position_risk_rows(
        positions,
        journal._read_state(),
        stop_loss_pct=args.stop_loss_pct,
        trailing_stop_pct=args.trailing_stop_pct,
    )
    if not risk_rows:
        print("none")
    for row in risk_rows:
        status = "TRIGGER" if row["distance_pct"] <= 0 else "watch"
        print(
            f"{row['symbol']:6} current=${row['current_price']:,.2f} high=${row['tracked_high']:,.2f} "
            f"stop=${row['active_stop']:,.2f} distance={row['distance_pct'] * 100:.2f}% {status}"
        )
    print()
    print("Open orders")
    if not open_orders:
        print("none")
    for order in open_orders:
        print(f"{order.symbol:6} {order.side.value.upper():4} qty={order.qty} status={order.status.value} id={order.client_order_id}")

    state = journal._read_state()
    print()
    print(f"Bot state account: {state.get('account_id', 'unknown')}")
    print(f"Submitted keys: {len(state.get('submitted_order_keys', []))}")
    print(f"Reconciliation: {'ok' if reconciliation.ok else 'FAILED'}")
    for message in reconciliation.messages:
        print(f"  {message}")

    if args.signals:
        print()
        result = _run_alpaca_once(args)
        _print_live_result(result, submitted=False)


def _daily_report(args) -> None:
    broker = AlpacaPaperBroker()
    clock = broker.market_clock()
    account = broker._client.get_account()
    positions = broker._client.get_all_positions()
    open_orders = _active_open_orders(broker._client.get_orders())
    journal = TradeJournal(args.journal_dir)
    synced = _sync_alpaca_protective_fills(journal, broker)
    reconciliation = reconcile_positions(journal, broker)
    _sync_position_highs_from_positions(journal, positions)
    state = journal._read_state()
    risk_rows = _position_risk_rows(
        positions,
        state,
        stop_loss_pct=args.stop_loss_pct,
        trailing_stop_pct=args.trailing_stop_pct,
    )
    report_date = _report_date(args)
    output = Path(args.output) if args.output else Path(args.journal_dir) / f"daily_report_{report_date.isoformat()}.md"

    lines = [
        f"# Trading Bot Report - {report_date.isoformat()}",
        "",
        f"- Lithuania time: {format_local(now_local())}",
        f"- Market open: {clock.is_open}",
        f"- Next close: {format_local(clock.next_close)}",
        f"- Equity: ${float(account.equity):,.2f}",
        f"- Cash: ${float(account.cash):,.2f}",
        f"- Buying power: ${float(account.buying_power):,.2f}",
        f"- Reconciliation: {'ok' if reconciliation.ok else 'FAILED'}",
        f"- Protective fills synced: {synced}",
        f"- Submitted keys: {len(state.get('submitted_order_keys', []))}",
        "",
        "## Positions",
        "",
    ]
    if not positions:
        lines.append("none")
    else:
        lines.extend(["| Symbol | Qty | Avg | Value | P/L | P/L % |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
        for position in positions:
            lines.append(
                f"| {position.symbol} | {position.qty} | ${float(position.avg_entry_price):,.2f} | "
                f"${float(position.market_value):,.2f} | ${float(position.unrealized_pl):,.2f} | "
                f"{float(position.unrealized_plpc) * 100:.2f}% |"
            )
    lines.extend(["", "## Risk", ""])
    if not risk_rows:
        lines.append("none")
    else:
        lines.extend(["| Symbol | Current | High | Stop | Distance |", "| --- | ---: | ---: | ---: | ---: |"])
        for row in risk_rows:
            lines.append(
                f"| {row['symbol']} | ${row['current_price']:,.2f} | ${row['tracked_high']:,.2f} | "
                f"${row['active_stop']:,.2f} | {row['distance_pct'] * 100:.2f}% |"
            )
    lines.extend(["", "## Open Orders", ""])
    if not open_orders:
        lines.append("none")
    else:
        lines.extend(["| Symbol | Side | Qty | Type | Status | Client ID |", "| --- | --- | ---: | --- | --- | --- |"])
        for order in open_orders:
            side = getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))
            order_type = getattr(getattr(order, "type", ""), "value", getattr(order, "type", ""))
            status = getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))
            lines.append(f"| {order.symbol} | {side} | {order.qty} | {order_type} | {status} | {order.client_order_id} |")
    if reconciliation.messages:
        lines.extend(["", "## Reconciliation Messages", ""])
        lines.extend(f"- {message}" for message in reconciliation.messages)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote report to {output}")


def _close_audit(args) -> None:
    print("Close audit")
    _alpaca_protect(args)

    health_path = _health_report(args)
    _daily_report(_namespace_with_output(args, None))
    _strategy_report(_namespace_with_output(args, None))

    if not args.skip_tests:
        import unittest

        print("Running tests")
        result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover("."))
        if not result.wasSuccessful():
            raise RuntimeError("Close audit tests failed.")
    print(f"Health report: {health_path}")


def _promotion_report(args) -> None:
    research_rows = _load_csv_rows(Path(args.research_csv))
    walk_rows = _load_csv_rows(Path(args.walk_forward_csv))
    decision = _promotion_decision(
        research_rows=research_rows,
        walk_rows=walk_rows,
        current=args.current,
        min_positive_folds=args.min_positive_folds,
        min_avg_edge_pct=args.min_avg_edge_pct,
        min_score_improvement=args.min_score_improvement,
        max_worst_drawdown_pct=args.max_worst_drawdown_pct,
    )
    output = Path(args.output)
    lines = [
        f"# Strategy Promotion Report - {now_local().date().isoformat()}",
        "",
        f"- Lithuania time: {format_local(now_local())}",
        f"- Current strategy: {args.current}",
        f"- Recommendation: {decision['recommendation']}",
        f"- Candidate: {decision['candidate']}",
        f"- Reason: {decision['reason']}",
        "",
        "## Current",
        "",
        *_promotion_row_lines(decision["current_row"]),
        "",
        "## Candidate",
        "",
        *_promotion_row_lines(decision["candidate_row"]),
        "",
        "## Thresholds",
        "",
        f"- Minimum positive folds: {args.min_positive_folds}",
        f"- Minimum average edge: {args.min_avg_edge_pct:.2f}%",
        f"- Minimum score improvement: {args.min_score_improvement:.2f}",
        f"- Maximum allowed worst drawdown: {args.max_worst_drawdown_pct:.2f}%",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote report to {output}")


def _risk_promotion_report(args) -> None:
    rows = _load_csv_rows(Path(args.risk_csv))
    decision = _risk_promotion_decision(
        rows=rows,
        current_stop_loss_pct=args.current_stop_loss_pct,
        current_trailing_stop_pct=args.current_trailing_stop_pct,
        min_return_improvement_pct=args.min_return_improvement_pct,
        min_score_improvement=args.min_score_improvement,
        max_drawdown_worsening_pct=args.max_drawdown_worsening_pct,
    )
    output = Path(args.output)
    lines = [
        f"# Risk Promotion Report - {now_local().date().isoformat()}",
        "",
        f"- Lithuania time: {format_local(now_local())}",
        f"- Current stop/trailing: {args.current_stop_loss_pct:.2%}/{args.current_trailing_stop_pct:.2%}",
        f"- Recommendation: {decision['recommendation']}",
        f"- Candidate stop/trailing: {decision['candidate_stop_loss_pct']:.2%}/{decision['candidate_trailing_stop_pct']:.2%}",
        f"- Reason: {decision['reason']}",
        "",
        "## Current",
        "",
        *_risk_row_lines(decision["current_row"]),
        "",
        "## Candidate",
        "",
        *_risk_row_lines(decision["candidate_row"]),
        "",
        "## Thresholds",
        "",
        f"- Minimum return improvement: {args.min_return_improvement_pct:.2f}%",
        f"- Minimum score improvement: {args.min_score_improvement:.2f}",
        f"- Maximum drawdown worsening: {args.max_drawdown_worsening_pct:.2f}%",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote report to {output}")


def _risk_promotion_decision(
    rows: list[dict[str, str]],
    current_stop_loss_pct: float,
    current_trailing_stop_pct: float,
    min_return_improvement_pct: float,
    min_score_improvement: float,
    max_drawdown_worsening_pct: float,
) -> dict[str, object]:
    if not rows:
        raise RuntimeError("Risk research CSV is empty.")
    current_row = _find_risk_row(rows, current_stop_loss_pct, current_trailing_stop_pct)
    candidate_row = max(rows, key=lambda row: _float(row, "score"))
    return_improvement = _float(candidate_row, "return_pct") - _float(current_row, "return_pct")
    score_improvement = _float(candidate_row, "score") - _float(current_row, "score")
    drawdown_worsening = abs(min(0.0, _float(candidate_row, "max_drawdown_pct"))) - abs(
        min(0.0, _float(current_row, "max_drawdown_pct"))
    )

    failures: list[str] = []
    if return_improvement < min_return_improvement_pct:
        failures.append("return improvement is too small")
    if score_improvement < min_score_improvement:
        failures.append("score improvement is too small")
    if drawdown_worsening > max_drawdown_worsening_pct:
        failures.append("drawdown worsens too much")

    recommendation = "PROMOTE" if not failures else "DO NOT PROMOTE"
    return {
        "recommendation": recommendation,
        "reason": "all thresholds passed" if not failures else "; ".join(failures),
        "candidate_stop_loss_pct": _float(candidate_row, "stop_loss_pct"),
        "candidate_trailing_stop_pct": _float(candidate_row, "trailing_stop_pct"),
        "current_row": current_row,
        "candidate_row": candidate_row,
    }


def _find_risk_row(rows: list[dict[str, str]], stop_loss_pct: float, trailing_stop_pct: float) -> dict[str, str]:
    for row in rows:
        if abs(_float(row, "stop_loss_pct") - stop_loss_pct) < 0.0001 and abs(
            _float(row, "trailing_stop_pct") - trailing_stop_pct
        ) < 0.0001:
            return row
    raise RuntimeError(f"Current risk setting {stop_loss_pct:.2%}/{trailing_stop_pct:.2%} not found in risk CSV.")


def _risk_row_lines(row: dict[str, str]) -> list[str]:
    keys = ["stop_loss_pct", "trailing_stop_pct", "return_pct", "max_drawdown_pct", "trades", "score"]
    return [f"- {key}: {row[key]}" for key in keys if key in row]


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"Missing CSV file: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _promotion_decision(
    research_rows: list[dict[str, str]],
    walk_rows: list[dict[str, str]],
    current: str,
    min_positive_folds: int,
    min_avg_edge_pct: float,
    min_score_improvement: float,
    max_worst_drawdown_pct: float,
) -> dict[str, object]:
    walk_by_strategy = {row["strategy"]: row for row in walk_rows}
    current_row = _find_strategy_row(walk_by_strategy, current)
    candidate_row = max(walk_rows, key=lambda row: (_float(row, "positive_folds"), _float(row, "avg_score")))

    failures: list[str] = []
    if _float(candidate_row, "positive_folds") < min_positive_folds:
        failures.append("not enough positive walk-forward folds")
    if _float(candidate_row, "avg_edge_pct") < min_avg_edge_pct:
        failures.append("average edge does not beat benchmark")
    if _float(candidate_row, "worst_drawdown_pct") < max_worst_drawdown_pct:
        failures.append("worst drawdown is too deep")
    current_score = _float(current_row, "avg_score") if current_row else 0.0
    score_improvement = _float(candidate_row, "avg_score") - current_score
    if score_improvement < min_score_improvement:
        failures.append("score improvement over current strategy is too small")

    recommendation = "PROMOTE" if not failures else "DO NOT PROMOTE"
    reason = "all thresholds passed" if not failures else "; ".join(failures)
    return {
        "recommendation": recommendation,
        "reason": reason,
        "candidate": candidate_row.get("strategy", "unknown"),
        "candidate_row": candidate_row,
        "current_row": current_row or {"strategy": current, "note": "current strategy not found in walk-forward CSV"},
        "research_rows": research_rows,
    }


def _find_strategy_row(rows_by_strategy: dict[str, dict[str, str]], strategy: str) -> dict[str, str] | None:
    if strategy in rows_by_strategy:
        return rows_by_strategy[strategy]
    if "-c" not in strategy:
        matches = [row for name, row in rows_by_strategy.items() if name.startswith(f"{strategy}-c")]
        if matches:
            return sorted(matches, key=lambda row: _float(row, "avg_score"), reverse=True)[0]
    base = strategy.rsplit("-c", 1)[0]
    return rows_by_strategy.get(base)


def _promotion_row_lines(row: dict[str, str]) -> list[str]:
    keys = [
        "strategy",
        "folds",
        "positive_folds",
        "avg_test_return_pct",
        "avg_benchmark_return_pct",
        "avg_edge_pct",
        "worst_drawdown_pct",
        "avg_sharpe",
        "total_trades",
        "avg_score",
        "note",
    ]
    return [f"- {key}: {row[key]}" for key in keys if key in row]


def _float(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, 0.0) or 0.0)
    except ValueError:
        return 0.0


def _namespace_with_output(args, output):
    values = vars(args).copy()
    values["output"] = output
    return argparse.Namespace(**values)


def _health_report(args) -> Path:
    broker = AlpacaPaperBroker()
    journal = TradeJournal(args.journal_dir)
    _sync_alpaca_protective_fills(journal, broker)
    positions = broker._client.get_all_positions()
    active_orders = _active_open_orders(broker._client.get_orders())
    _sync_position_highs_from_positions(journal, positions)
    state = journal._read_state()
    reconciliation = reconcile_positions(journal, broker)
    health = _health_scorecard(
        positions=positions,
        active_orders=active_orders,
        state=state,
        reconciliation_ok=reconciliation.ok,
        stop_loss_pct=args.stop_loss_pct,
        trailing_stop_pct=args.trailing_stop_pct,
    )
    report_date = _report_date(args)
    output = Path(args.journal_dir) / f"health_report_{report_date.isoformat()}.md"
    lines = [
        f"# Health Report - {report_date.isoformat()}",
        "",
        f"- Lithuania time: {format_local(now_local())}",
        f"- Overall: {health['overall']}",
        f"- Score: {health['score']}/{health['max_score']}",
        f"- Reconciliation: {'ok' if reconciliation.ok else 'FAILED'}",
        f"- Positions: {health['position_count']}",
        f"- Protected positions: {health['protected_count']}",
        f"- Missing stops: {', '.join(health['missing_stops']) if health['missing_stops'] else 'none'}",
        f"- Stale stops: {', '.join(health['stale_stops']) if health['stale_stops'] else 'none'}",
        f"- Duplicate sell stops: {', '.join(health['duplicate_sell_stops']) if health['duplicate_sell_stops'] else 'none'}",
    ]
    if reconciliation.messages:
        lines.extend(["", "## Reconciliation Messages", ""])
        lines.extend(f"- {message}" for message in reconciliation.messages)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote report to {output}")
    return output


def _health_scorecard(
    positions,
    active_orders,
    state: dict[str, object],
    reconciliation_ok: bool,
    stop_loss_pct: float,
    trailing_stop_pct: float,
) -> dict[str, object]:
    held_symbols = {str(position.symbol).upper() for position in positions}
    sell_orders_by_symbol: dict[str, list[object]] = {}
    for order in active_orders:
        side = getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))
        if str(side).lower() != Side.SELL.value:
            continue
        sell_orders_by_symbol.setdefault(str(order.symbol).upper(), []).append(order)
    risk_rows = _position_risk_rows(positions, state, stop_loss_pct, trailing_stop_pct)
    risk_by_symbol = {str(row["symbol"]).upper(): row for row in risk_rows}
    missing_stops = sorted(symbol for symbol in held_symbols if symbol not in sell_orders_by_symbol)
    duplicate_sell_stops = sorted(symbol for symbol, orders in sell_orders_by_symbol.items() if len(orders) > 1)
    stale_stops: list[str] = []
    for symbol, orders in sell_orders_by_symbol.items():
        row = risk_by_symbol.get(symbol)
        if not row:
            continue
        stop_price = max(float(getattr(order, "stop_price", 0.0) or 0.0) for order in orders)
        expected_stop = float(row["active_stop"])
        if expected_stop > stop_price + 0.05:
            stale_stops.append(symbol)
    checks = [
        reconciliation_ok,
        not missing_stops,
        not duplicate_sell_stops,
        not stale_stops,
    ]
    score = sum(1 for check in checks if check)
    return {
        "overall": "ok" if score == len(checks) else "ATTENTION",
        "score": score,
        "max_score": len(checks),
        "position_count": len(held_symbols),
        "protected_count": len(held_symbols - set(missing_stops)),
        "missing_stops": missing_stops,
        "duplicate_sell_stops": duplicate_sell_stops,
        "stale_stops": sorted(stale_stops),
    }


def _strategy_report(args) -> None:
    broker = AlpacaPaperBroker()
    journal = TradeJournal(args.journal_dir)
    _sync_alpaca_protective_fills(journal, broker)
    fills = _load_journal_fills(journal.fills_path)
    current_prices = dict(broker.position_prices)
    benchmark_symbol = args.benchmark.upper()
    benchmark_prices = _benchmark_prices(benchmark_symbol, args.days, args.feed)
    rows = _strategy_trade_rows(fills, current_prices, benchmark_prices)
    summary = _strategy_trade_summary(rows)
    output = Path(args.output) if args.output else Path(args.journal_dir) / f"strategy_report_{now_local().date().isoformat()}.md"

    lines = [
        f"# Strategy Report - {now_local().date().isoformat()}",
        "",
        f"- Lithuania time: {format_local(now_local())}",
        f"- Benchmark: {benchmark_symbol}",
        f"- Closed trades: {summary['closed_count']}",
        f"- Open trades: {summary['open_count']}",
        f"- Realized P/L: ${summary['realized_pl']:,.2f}",
        f"- Open P/L: ${summary['open_pl']:,.2f}",
        f"- Total tracked P/L: ${summary['total_pl']:,.2f}",
        f"- Average trade return: {summary['avg_trade_return'] * 100:.2f}%",
        f"- Average benchmark return: {summary['avg_benchmark_return'] * 100:.2f}%",
        f"- Average excess return: {summary['avg_excess_return'] * 100:.2f}%",
        f"- Win rate: {summary['win_rate'] * 100:.2f}%",
        f"- Average win: ${summary['avg_win']:,.2f}",
        f"- Average loss: ${summary['avg_loss']:,.2f}",
        f"- Average closed holding days: {summary['avg_closed_holding_days']:.2f}",
        "",
        "## Signal Groups",
        "",
    ]
    group_rows = _strategy_reason_groups(rows)
    if not group_rows:
        lines.append("No signal groups yet.")
    else:
        lines.extend(["| Signal Ingredient | Trades | Avg Return | Avg Excess | Total P/L |", "| --- | ---: | ---: | ---: | ---: |"])
        for group in group_rows:
            lines.append(
                f"| {group['reason']} | {group['count']} | {group['avg_return'] * 100:.2f}% | "
                f"{group['avg_excess'] * 100:.2f}% | ${group['total_pl']:,.2f} |"
            )
    lines.extend(
        [
            "",
            "## Trades",
            "",
        ]
    )
    if not rows:
        lines.append("No strategy fills recorded yet.")
    else:
        lines.extend(
            [
                "| Symbol | Status | Qty | Entry Date | Entry | Exit/Current Date | Exit/Current | P/L | P/L % | Holding Days | Reason |",
                "| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for row in rows:
            lines.append(
                f"| {row['symbol']} | {row['status']} | {row['quantity']} | {row['entry_date']} | "
                f"${row['entry_price']:,.2f} | {row['exit_date']} | ${row['exit_price']:,.2f} | "
                f"${row['pl']:,.2f} | {row['pl_pct'] * 100:.2f}% "
                f"({row['excess_return'] * 100:+.2f}% vs benchmark) | {row['holding_days']} | {row['reason']} |"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote report to {output}")


def _load_journal_fills(path: Path) -> list[Fill]:
    if not path.exists():
        return []
    fills: list[Fill] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            symbol = str(row.get("symbol", "")).upper()
            side = Side(str(row.get("side", "")).lower())
            quantity = int(float(row.get("quantity") or 0))
            price = float(row.get("price") or 0)
            trade_date = date.fromisoformat(str(row.get("date", "")))
            if quantity <= 0 or price <= 0:
                continue
            fills.append(
                Fill(
                    date=trade_date,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    value=quantity * price,
                    reason=str(row.get("reason", "")),
                )
            )
    return fills


def _strategy_reason_groups(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        for reason in _reason_ingredients(str(row.get("reason", ""))):
            groups.setdefault(reason, []).append(row)
    output = []
    for reason, reason_rows in groups.items():
        output.append(
            {
                "reason": reason,
                "count": len(reason_rows),
                "avg_return": sum(float(row["pl_pct"]) for row in reason_rows) / len(reason_rows),
                "avg_excess": sum(float(row.get("excess_return", 0.0)) for row in reason_rows) / len(reason_rows),
                "total_pl": sum(float(row["pl"]) for row in reason_rows),
            }
        )
    return sorted(output, key=lambda row: (float(row["avg_excess"]), float(row["total_pl"])), reverse=True)


def _reason_ingredients(reason: str) -> list[str]:
    cleaned = reason.replace("submitted to Alpaca paper:", "").strip()
    if "synced Alpaca protective stop" in cleaned:
        return ["protective stop exit"]
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1].strip()
    ingredients = [part.strip() for part in cleaned.split(";")]
    return [part for part in ingredients if part]


def _benchmark_prices(symbol: str, days: int, feed: str) -> dict[date, float]:
    try:
        rows = AlpacaMarketDataProvider(days=days, feed=feed).load([symbol]).get(symbol, [])
    except Exception:
        return {}
    return {row.date: row.close for row in rows}


def _strategy_trade_rows(
    fills: list[Fill],
    current_prices: dict[str, float],
    benchmark_prices: dict[date, float] | None = None,
) -> list[dict[str, object]]:
    benchmark_prices = benchmark_prices or {}
    lots: dict[str, list[Fill]] = {}
    rows: list[dict[str, object]] = []
    for fill in sorted(fills, key=lambda item: item.date):
        if fill.side is Side.BUY:
            lots.setdefault(fill.symbol, []).append(fill)
            continue
        remaining_sell = fill.quantity
        while remaining_sell > 0 and lots.get(fill.symbol):
            buy = lots[fill.symbol][0]
            matched_quantity = min(remaining_sell, buy.quantity)
            rows.append(
                _strategy_trade_row(
                    buy,
                    matched_quantity,
                    fill.price,
                    fill.date,
                    "closed",
                    fill.reason,
                    benchmark_prices,
                )
            )
            remaining_sell -= matched_quantity
            if matched_quantity == buy.quantity:
                lots[fill.symbol].pop(0)
            else:
                lots[fill.symbol][0] = Fill(
                    date=buy.date,
                    symbol=buy.symbol,
                    side=buy.side,
                    quantity=buy.quantity - matched_quantity,
                    price=buy.price,
                    value=(buy.quantity - matched_quantity) * buy.price,
                    reason=buy.reason,
                )

    for symbol, open_lots in sorted(lots.items()):
        current_price = current_prices.get(symbol)
        if current_price is None:
            continue
        for buy in open_lots:
            rows.append(
                _strategy_trade_row(
                    buy,
                    buy.quantity,
                    current_price,
                    now_local().date(),
                    "open",
                    buy.reason,
                    benchmark_prices,
                )
            )
    return rows


def _strategy_trade_row(
    buy: Fill,
    quantity: int,
    exit_price: float,
    exit_date: date,
    status: str,
    reason: str,
    benchmark_prices: dict[date, float] | None = None,
) -> dict[str, object]:
    pl = (exit_price - buy.price) * quantity
    entry_value = buy.price * quantity
    pl_pct = pl / entry_value if entry_value else 0.0
    benchmark_return = _benchmark_return(benchmark_prices or {}, buy.date, exit_date)
    return {
        "symbol": buy.symbol,
        "status": status,
        "quantity": quantity,
        "entry_date": buy.date.isoformat(),
        "entry_price": buy.price,
        "exit_date": exit_date.isoformat(),
        "exit_price": exit_price,
        "pl": pl,
        "pl_pct": pl_pct,
        "benchmark_return": benchmark_return,
        "excess_return": pl_pct - benchmark_return,
        "holding_days": (exit_date - buy.date).days,
        "reason": reason,
    }


def _benchmark_return(prices: dict[date, float], entry_date: date, exit_date: date) -> float:
    entry_price = _price_on_or_after(prices, entry_date)
    exit_price = _price_on_or_before(prices, exit_date)
    if entry_price is None or exit_price is None or entry_price <= 0:
        return 0.0
    return (exit_price - entry_price) / entry_price


def _price_on_or_after(prices: dict[date, float], target_date: date) -> float | None:
    for price_date in sorted(prices):
        if price_date >= target_date:
            return prices[price_date]
    return None


def _price_on_or_before(prices: dict[date, float], target_date: date) -> float | None:
    for price_date in sorted(prices, reverse=True):
        if price_date <= target_date:
            return prices[price_date]
    return None


def _strategy_trade_summary(rows: list[dict[str, object]]) -> dict[str, float | int]:
    closed = [row for row in rows if row["status"] == "closed"]
    open_rows = [row for row in rows if row["status"] == "open"]
    wins = [float(row["pl"]) for row in closed if float(row["pl"]) > 0]
    losses = [float(row["pl"]) for row in closed if float(row["pl"]) < 0]
    realized_pl = sum(float(row["pl"]) for row in closed)
    open_pl = sum(float(row["pl"]) for row in open_rows)
    trade_returns = [float(row["pl_pct"]) for row in rows]
    benchmark_returns = [float(row.get("benchmark_return", 0.0)) for row in rows]
    excess_returns = [float(row.get("excess_return", 0.0)) for row in rows]
    return {
        "closed_count": len(closed),
        "open_count": len(open_rows),
        "realized_pl": realized_pl,
        "open_pl": open_pl,
        "total_pl": realized_pl + open_pl,
        "avg_trade_return": sum(trade_returns) / len(trade_returns) if trade_returns else 0.0,
        "avg_benchmark_return": sum(benchmark_returns) / len(benchmark_returns) if benchmark_returns else 0.0,
        "avg_excess_return": sum(excess_returns) / len(excess_returns) if excess_returns else 0.0,
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "avg_closed_holding_days": (
            sum(int(row["holding_days"]) for row in closed) / len(closed) if closed else 0.0
        ),
    }


def _report_date(args) -> date:
    raw_date = getattr(args, "report_date", None)
    if not raw_date:
        return now_local().date()
    try:
        return date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError("--report-date must be in YYYY-MM-DD format") from exc


def _position_risk_rows(
    positions,
    state: dict,
    stop_loss_pct: float,
    trailing_stop_pct: float,
) -> list[dict[str, float | str]]:
    highs = state.get("position_highs", {})
    rows: list[dict[str, float | str]] = []
    for position in positions:
        symbol = str(position.symbol).upper()
        quantity = float(position.qty)
        if quantity <= 0:
            continue
        average_cost = float(position.avg_entry_price)
        current_price = float(position.market_value) / quantity
        tracked_high = max(float(highs.get(symbol, 0.0) or 0.0), average_cost, current_price)
        fixed_stop = average_cost * (1 - stop_loss_pct)
        trailing_stop = tracked_high * (1 - trailing_stop_pct) if tracked_high > average_cost else 0.0
        active_stop = max(fixed_stop, trailing_stop)
        distance_pct = (current_price - active_stop) / current_price if current_price > 0 else 0.0
        rows.append(
            {
                "symbol": symbol,
                "current_price": current_price,
                "tracked_high": tracked_high,
                "active_stop": active_stop,
                "distance_pct": distance_pct,
            }
        )
    return rows


def _sync_position_highs_from_positions(journal: TradeJournal, positions) -> dict[str, float]:
    prices: dict[str, float] = {}
    held_symbols: set[str] = set()
    for position in positions:
        symbol = str(position.symbol).upper()
        quantity = float(position.qty)
        if quantity <= 0:
            continue
        current_price = float(position.market_value) / quantity
        prices[symbol] = current_price
        held_symbols.add(symbol)
    return journal.update_position_highs(prices, held_symbols)


def _minutes_since_regular_open(timestamp) -> float:
    market_now = timestamp.astimezone(MARKET_TZ)
    regular_open = market_now.replace(
        hour=day_time(9, 30).hour,
        minute=day_time(9, 30).minute,
        second=0,
        microsecond=0,
    )
    return (market_now - regular_open).total_seconds() / 60


def _reset_state(args) -> None:
    if not args.yes:
        raise RuntimeError("Reset not confirmed. Re-run with --yes to archive and reset local bot state.")
    broker = AlpacaPaperBroker()
    account = broker._client.get_account()
    if broker.positions:
        raise RuntimeError("Refusing to reset state while Alpaca has open positions.")
    if broker._client.get_orders():
        raise RuntimeError("Refusing to reset state while Alpaca has open orders.")
    journal = TradeJournal(args.journal_dir)
    archive_dir = journal.reset_for_account(getattr(broker, "account_id", None), float(account.equity))
    print(f"Archived old bot runtime state to {archive_dir}")
    print(f"Reset local bot state for account {broker.account_id}")


def _print_result(config: BotConfig, result) -> None:
    print("AI Stock Trading Bot - paper run")
    print(f"Symbols: {', '.join(config.symbols)}")
    print(f"Starting cash: ${config.starting_cash:,.2f}")
    print(f"Final equity: ${result.final_equity:,.2f}")
    print(f"Return: {result.return_pct:.2f}%")
    print(f"Trades: {len(result.fills)}")

    if result.fills:
        print()
        print("Recent fills:")
        for fill in result.fills[-10:]:
            print(
                f"{fill.date} {fill.side.value.upper():4} "
                f"{fill.quantity:4} {fill.symbol:6} @ ${fill.price:8.2f} "
                f"${fill.value:10.2f}  {fill.reason}"
            )


def _print_live_result(result, submitted: bool) -> None:
    print("AI Stock Trading Bot - Alpaca paper latest signal")
    print(f"Account equity: ${result.equity:,.2f}")
    print(f"Mode: {'submit paper orders' if submitted else 'dry run'}")
    print()

    for decision in result.decisions:
        signal = decision.signal
        order = decision.order
        action = "NO ORDER"
        if order:
            action = f"{order.side.value.upper()} {order.quantity} {order.symbol} @ approx ${order.price:.2f}"
        elif decision.rejected_reason:
            action = f"REJECTED: {decision.rejected_reason}"
        print(
            f"{signal.symbol:6} {signal.side.value.upper():4} "
            f"confidence={signal.confidence:.2f}  {action}  {signal.reason}"
        )

    if submitted and any(decision.fill for decision in result.decisions):
        print()
        print("Submitted fills/order requests:")
        for decision in result.decisions:
            if decision.fill:
                fill = decision.fill
                print(f"{fill.date} {fill.side.value.upper()} {fill.quantity} {fill.symbol}  {fill.reason}")


def _print_research(rows, symbols: list[str]) -> None:
    print()
    print(f"Evaluated universe: {', '.join(symbols)}")
    print("Top strategies by test score")
    print(
        f"{'strategy':22} {'train%':>8} {'test%':>8} {'bench%':>8} {'edge%':>8} "
        f"{'dd%':>8} {'sharpe':>8} {'trades':>7} {'score':>8}"
    )
    for row in rows:
        edge = row.test.return_pct - row.benchmark_return_pct
        print(
            f"{row.spec.name:22} "
            f"{row.train.return_pct:8.2f} "
            f"{row.test.return_pct:8.2f} "
            f"{row.benchmark_return_pct:8.2f} "
            f"{edge:8.2f} "
            f"{row.test.max_drawdown_pct:8.2f} "
            f"{row.test.sharpe:8.2f} "
            f"{row.test.trades:7} "
            f"{row.test.score:8.2f}"
        )


def _print_walk_forward(rows) -> None:
    print()
    print("Walk-forward validation")
    print(
        f"{'strategy':22} {'folds':>5} {'pos':>5} {'test%':>8} {'bench%':>8} "
        f"{'edge%':>8} {'worstdd':>8} {'sharpe':>8} {'trades':>7} {'score':>8}"
    )
    for row in rows:
        print(
            f"{row.spec_name:22} "
            f"{row.folds:5} "
            f"{row.positive_folds:5} "
            f"{row.avg_test_return_pct:8.2f} "
            f"{row.avg_benchmark_return_pct:8.2f} "
            f"{row.avg_edge_pct:8.2f} "
            f"{row.worst_drawdown_pct:8.2f} "
            f"{row.avg_sharpe:8.2f} "
            f"{row.total_trades:7} "
            f"{row.avg_score:8.2f}"
        )


def _print_risk_research(rows) -> None:
    print()
    print("Top stop/trailing settings")
    print(f"{'stop%':>8} {'trail%':>8} {'return%':>9} {'dd%':>8} {'trades':>7} {'score':>9}")
    for row in rows:
        print(
            f"{row['stop_loss_pct'] * 100:8.2f} "
            f"{row['trailing_stop_pct'] * 100:8.2f} "
            f"{row['return_pct']:9.2f} "
            f"{row['max_drawdown_pct']:8.2f} "
            f"{row['trades']:7} "
            f"{row['score']:9.2f}"
        )


def _write_research_csv(path: str, rows) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "strategy",
                "train_return_pct",
                "test_return_pct",
                "benchmark_return_pct",
                "edge_pct",
                "test_max_drawdown_pct",
                "test_sharpe",
                "test_trades",
                "test_score",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.spec.name,
                    f"{row.train.return_pct:.4f}",
                    f"{row.test.return_pct:.4f}",
                    f"{row.benchmark_return_pct:.4f}",
                    f"{row.test.return_pct - row.benchmark_return_pct:.4f}",
                    f"{row.test.max_drawdown_pct:.4f}",
                    f"{row.test.sharpe:.4f}",
                    row.test.trades,
                    f"{row.test.score:.4f}",
                ]
            )
    print(f"Wrote research results to {path}")


def _write_risk_research_csv(path: str, rows: list[dict[str, float]]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "stop_loss_pct",
                "trailing_stop_pct",
                "return_pct",
                "max_drawdown_pct",
                "trades",
                "score",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    f"{row['stop_loss_pct']:.4f}",
                    f"{row['trailing_stop_pct']:.4f}",
                    f"{row['return_pct']:.4f}",
                    f"{row['max_drawdown_pct']:.4f}",
                    row["trades"],
                    f"{row['score']:.4f}",
                ]
            )
    print(f"Wrote risk research results to {path}")


def _write_walk_forward_csv(path: str, rows) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "strategy",
                "folds",
                "positive_folds",
                "avg_test_return_pct",
                "avg_benchmark_return_pct",
                "avg_edge_pct",
                "worst_drawdown_pct",
                "avg_sharpe",
                "total_trades",
                "avg_score",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.spec_name,
                    row.folds,
                    row.positive_folds,
                    f"{row.avg_test_return_pct:.4f}",
                    f"{row.avg_benchmark_return_pct:.4f}",
                    f"{row.avg_edge_pct:.4f}",
                    f"{row.worst_drawdown_pct:.4f}",
                    f"{row.avg_sharpe:.4f}",
                    row.total_trades,
                    f"{row.avg_score:.4f}",
                ]
            )
    print(f"Wrote walk-forward results to {path}")


def _build_strategy(name: str, config: BotConfig):
    ma = MovingAverageCrossStrategy(config.short_window, config.long_window)
    if name == "ma":
        return ma
    if name == "momentum":
        return MomentumStrategy(lookback=config.long_window)
    if name == "mean-reversion":
        return MeanReversionStrategy(window=config.long_window)
    if name == "relative-strength":
        return RegimeRelativeStrengthStrategy()
    return EnsembleStrategy(
        [
            ma,
            MomentumStrategy(lookback=config.long_window),
            MeanReversionStrategy(window=config.long_window),
        ]
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
