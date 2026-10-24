from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import csv
import math
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from trading_bot.config import BotConfig
from trading_bot.data import DemoMarketDataProvider, StaticMarketDataProvider, validate_market_data
from trading_bot.engine import RunResult, TradingEngine
from trading_bot.models import Candle, PortfolioSnapshot, Side, Signal
from trading_bot.research import (
    StrategySpec, assess_research_evidence, build_research_manifest,
    buy_and_hold_return, evaluate_specs, evaluate_walk_forward, metrics, split_market_data,
)


START = date(2020, 1, 1)


def bars(symbol="A", count=10, offset=0):
    return [Candle(START + timedelta(days=i), symbol, 100, 100, 100, 100, 1000)
            for i in range(offset, offset + count)]


class BuyStrategy:
    def generate(self, symbol, history):
        return Signal(symbol, Side.BUY, 0.95, "test entry")


class PassAdvisor:
    def review(self, signal, history):
        return signal


def run(data, *, trade_start=None, **kwargs):
    config = BotConfig(symbols=list(data), slippage_bps=0, stop_loss_pct=0.9,
                       trailing_stop_pct=0.9, **kwargs)
    return TradingEngine(config, StaticMarketDataProvider(data), BuyStrategy(), PassAdvisor(),
                         trade_start_date=trade_start).run()


class ResearchIntegrityTests(unittest.TestCase):
    def test_duplicate_bars_and_missing_symbols_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate candle"):
            validate_market_data({"A": bars(count=1) * 2})
        with self.assertRaisesRegex(ValueError, "No market data"):
            validate_market_data({"A": bars()}, ["A", "B"])

    def test_invalid_provider_bar_is_rejected_before_trading(self):
        # Simulate a provider bypassing dataclass construction validation.
        bar = bars(count=1)[0]
        object.__setattr__(bar, "close", math.nan)
        with self.assertRaisesRegex(ValueError, "Invalid price"):
            validate_market_data({"A": [bar]})

    def test_demo_does_not_change_global_random_state(self):
        random.seed(42)
        state = random.getstate()
        DemoMarketDataProvider(days=10).load(["A"])
        self.assertEqual(random.getstate(), state)

    def test_train_split_uses_one_calendar_boundary_for_ragged_symbols(self):
        data = {"A": bars(count=10), "B": bars("B", count=5, offset=5)}
        train, test = split_market_data(data, 0.6)
        self.assertEqual([bar.date for bar in train["B"]], [START + timedelta(days=5)])
        self.assertTrue(all(bar.date < START + timedelta(days=6) for rows in train.values() for bar in rows))
        self.assertEqual(test["B"][-1].date, START + timedelta(days=9))

    def test_bad_split_and_fold_parameters_fail_explicitly(self):
        data = {"A": bars(count=121)}
        config = BotConfig(symbols=["A"])
        for fraction in (0, 1, math.nan):
            with self.assertRaises(ValueError):
                split_market_data(data, fraction)
        for folds in (0, -1, 100, 1.5):
            with self.assertRaises(ValueError):
                evaluate_walk_forward(data, ["A"], config, folds=folds)
        with self.assertRaises(ValueError):
            split_market_data({"A": bars(count=1)}, 0.65)

    def test_candidate_ranking_never_uses_holdout_score(self):
        def result(return_pct):
            return RunResult([
                PortfolioSnapshot(START, 100, 0, 100),
                PortfolioSnapshot(START + timedelta(days=1), 100 + return_pct, 0, 100 + return_pct),
            ], [])
        specs = [StrategySpec("training-winner", 5, 20, 0.55, BuyStrategy()),
                 StrategySpec("holdout-winner", 5, 20, 0.55, BuyStrategy())]
        with patch("trading_bot.research._run_static", side_effect=[result(10), result(-10), result(5), result(20)]):
            rows = evaluate_specs({"A": bars()}, ["A"], BotConfig(symbols=["A"]), specs)
        self.assertEqual(rows[0].spec.name, "training-winner")

    def test_research_preserves_no_new_buys_configuration(self):
        specs = [StrategySpec("buy", 5, 20, 0.55, BuyStrategy())]
        config = BotConfig(symbols=["A"], allow_new_buys=False)
        rows = evaluate_specs({"A": bars(count=40)}, ["A"], config, specs)
        self.assertEqual(rows[0].train.trades, 0)
        self.assertEqual(rows[0].test.trades, 0)

    def test_research_does_not_share_mutable_strategy_state_between_samples(self):
        class CountingStrategy(BuyStrategy):
            def __init__(self):
                self.calls = 0

            def generate(self, symbol, history):
                self.calls += 1
                return super().generate(symbol, history)

        strategy = CountingStrategy()
        specs = [StrategySpec("counting", 5, 20, 0.55, strategy)]
        evaluate_specs({"A": bars(count=40)}, ["A"], BotConfig(symbols=["A"]), specs)
        self.assertEqual(strategy.calls, 0)

    def test_walk_forward_uses_every_test_date_exactly_once(self):
        data = {"A": bars(count=121)}
        windows = []

        def fake_run(data, config, strategy, trade_start_date=None):
            test_bars = [bar for bar in data["A"] if bar.date >= trade_start_date]
            windows.extend(bar.date for bar in test_bars)
            return RunResult([PortfolioSnapshot(bar.date, 100, 0, 100) for bar in test_bars], [])

        specs = [StrategySpec("buy", 5, 20, 0.55, BuyStrategy())]
        with patch("trading_bot.research._run_static", side_effect=fake_run):
            rows = evaluate_walk_forward(data, ["A"], BotConfig(symbols=["A"]), specs, folds=3)
        self.assertEqual(windows, [bar.date for bar in data["A"][int(121 * 0.45):]])
        self.assertEqual(rows[0].folds, 3)

    def test_warmup_is_excluded_from_performance_and_sharpe(self):
        data = bars(count=30)
        for i in range(20, 30):
            price = 100 + i - 20
            data[i] = replace(data[i], open=price, high=price, low=price, close=price)
        test_start = data[20].date
        warmed = run({"A": data}, trade_start=test_start)
        no_warmup = run({"A": data[20:]}, trade_start=test_start)
        self.assertEqual(warmed.snapshots[0].date, test_start)
        self.assertEqual(len(warmed.snapshots), 10)
        self.assertEqual(metrics(warmed), metrics(no_warmup))

    def test_drawdown_exit_fills_next_open_and_latches(self):
        data = [
            Candle(START, "A", 100, 100, 100, 100, 1000),
            Candle(START + timedelta(days=1), "A", 100, 100, 90, 90, 1000),
            Candle(START + timedelta(days=2), "A", 80, 200, 80, 200, 1000),
            Candle(START + timedelta(days=3), "A", 200, 200, 200, 200, 1000),
        ]
        result = run({"A": data}, max_order_pct=1, max_position_pct=1, target_cash_pct=0,
                     max_drawdown_stop_pct=0.04)
        self.assertEqual([fill.side for fill in result.fills], [Side.BUY, Side.SELL])
        self.assertEqual(result.fills[1].date, START + timedelta(days=2))
        self.assertEqual(result.fills[1].price, 80)
        self.assertEqual(result.fills[1].reason, "account drawdown stop")

    def test_missing_session_expires_order_and_does_not_regenerate_stale_signal(self):
        data = {"A": [bars(count=1)[0], bars(count=1, offset=3)[0]], "B": bars("B", count=4)}
        result = run(data)
        self.assertFalse(any(fill.symbol == "A" for fill in result.fills))

    def test_opening_gap_resizes_order_to_current_risk_budget(self):
        data = {"A": [Candle(START, "A", 100, 100, 100, 100, 1000),
                      Candle(START + timedelta(days=1), "A", 200, 200, 200, 200, 1000)]}
        result = run(data, max_order_pct=0.1)
        self.assertEqual(result.fills[0].quantity, 5)
        self.assertEqual(result.fills[0].value, 1000)

    def test_trailing_high_survives_a_missing_symbol_bar(self):
        data = {
            "A": [Candle(START, "A", 100, 100, 100, 100, 1000),
                  Candle(START + timedelta(days=1), "A", 100, 120, 100, 119, 1000),
                  Candle(START + timedelta(days=3), "A", 105, 105, 104, 105, 1000)],
            "B": bars("B", count=4),
        }
        config = BotConfig(symbols=["A", "B"], slippage_bps=0, trailing_stop_pct=0.05)
        result = TradingEngine(config, StaticMarketDataProvider(data), BuyStrategy(), PassAdvisor()).run()
        exits = [fill for fill in result.fills if fill.symbol == "A" and fill.side is Side.SELL]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].reason, "trailing stop")
        self.assertEqual(exits[0].price, 105)

    def test_benchmark_uses_next_open_and_includes_entry_costs(self):
        data = {"A": [Candle(START, "A", 100, 100, 100, 100, 1000),
                      Candle(START + timedelta(days=1), "A", 200, 200, 200, 200, 1000)]}
        self.assertEqual(buy_and_hold_return(data, 1000), 0)
        cost_return = buy_and_hold_return(data, 1000, slippage_bps=100, commission_per_trade=10)
        self.assertAlmostEqual(cost_return, ((990 / 202 * 200) / 1000 - 1) * 100)

    def test_manifest_fingerprint_changes_with_market_data(self):
        config = BotConfig(symbols=["A"])
        specs = [StrategySpec("buy", 5, 20, 0.55, BuyStrategy())]
        first = build_research_manifest({"A": bars()}, ["A"], config, specs, data_source="csv")
        changed = bars()
        changed[-1] = replace(changed[-1], volume=2000)
        second = build_research_manifest({"A": changed}, ["A"], config, specs, data_source="csv")
        self.assertNotEqual(first["data"]["sha256"], second["data"]["sha256"])
        self.assertFalse(first["profitability_proven"])
        self.assertFalse(first["holdout_verified"])

    def test_legacy_or_nonfinite_evidence_never_qualifies(self):
        row = {"strategy": "buy", "test_return_pct": "nan", "benchmark_return_pct": "3",
               "test_max_drawdown_pct": "-2", "test_trades": "30"}
        walk = {"strategy": "buy", "folds": "4", "positive_folds": "4", "avg_edge_pct": "5",
                "worst_drawdown_pct": "-5", "total_trades": "100"}
        assessed = assess_research_evidence([row], [walk])
        self.assertEqual(assessed["status"], "insufficient_evidence")
        self.assertFalse(assessed["eligible_for_live"])
        self.assertTrue(any("test_return_pct" in reason for reason in assessed["blockers"]))

    def test_malformed_manifest_sections_return_insufficient_evidence(self):
        assessed = assess_research_evidence([], [], manifest={
            "schema_version": 1, "evaluation": None, "data": [], "config": "invalid",
        })
        self.assertEqual(assessed["status"], "insufficient_evidence")
        self.assertTrue(any("Invalid provenance section" in reason for reason in assessed["blockers"]))

    def test_positive_research_remains_exploratory(self):
        config = BotConfig(symbols=["A"])
        specs = [StrategySpec("buy", 5, 20, 0.55, BuyStrategy())]
        manifest = build_research_manifest({"A": bars()}, ["A"], config, specs, data_source="csv")
        row = {"strategy": "buy", "test_return_pct": "10", "benchmark_return_pct": "3",
               "test_max_drawdown_pct": "-2", "test_trades": "30"}
        walk = {"strategy": "buy", "folds": "4", "positive_folds": "4", "avg_edge_pct": "5",
                "worst_drawdown_pct": "-5", "total_trades": "100"}
        assessed = assess_research_evidence([row], [walk], manifest=manifest)
        self.assertEqual(assessed["status"], "research_candidate")
        self.assertFalse(assessed["profitability_proven"])
        self.assertFalse(assessed["eligible_for_live"])
        manifest["data_source"] = "demo"
        self.assertEqual(assess_research_evidence([row], [walk], manifest=manifest)["status"], "insufficient_evidence")

    def test_latest_legacy_results_fail_performance_checks_even_with_provenance(self):
        root = Path(__file__).resolve().parents[1]
        with (root / "research_v6_circuit10_2026-08-17.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        with (root / "research_v6_circuit10_walkforward_2026-08-17.csv").open(newline="", encoding="utf-8") as handle:
            walk = list(csv.DictReader(handle))
        # Hypothetical provenance must not hide the negative edge in legacy rows.
        manifest = build_research_manifest(
            {"A": bars(count=300)}, ["A"], BotConfig(symbols=["A"]), [], data_source="csv", folds=6
        )
        assessed = assess_research_evidence(rows, walk, manifest=manifest)
        self.assertEqual(assessed["status"], "fails_research_checks")
        self.assertAlmostEqual(assessed["metrics"]["test_edge_pct"], -18.5867)
        self.assertTrue(any("Holdout does not outperform" in reason for reason in assessed["blockers"]))
        self.assertTrue(any("walk-forward edge" in reason for reason in assessed["blockers"]))
        self.assertTrue(any("drawdown exceeds" in reason for reason in assessed["blockers"]))

    def test_weak_metrics_cannot_pass_with_a_valid_manifest(self):
        manifest = build_research_manifest(
            {"A": bars(count=300)}, ["A"], BotConfig(symbols=["A"]), [], data_source="csv", folds=4
        )
        good_row = {"strategy": "buy", "test_return_pct": "10", "benchmark_return_pct": "3",
                    "test_max_drawdown_pct": "-2", "test_trades": "30"}
        good_walk = {"strategy": "buy", "folds": "4", "positive_folds": "4", "avg_edge_pct": "5",
                     "worst_drawdown_pct": "-5", "total_trades": "100"}
        for metric, value, is_walk in (
            ("test_return_pct", "-1", False), ("test_return_pct", "2", False),
            ("test_trades", "0", False), ("test_max_drawdown_pct", "-30", False),
            ("avg_edge_pct", "-1", True), ("folds", "2", True),
            ("positive_folds", "1", True), ("worst_drawdown_pct", "-30", True),
            ("total_trades", "0", True),
        ):
            with self.subTest(metric=metric, value=value):
                row, walk = dict(good_row), dict(good_walk)
                (walk if is_walk else row)[metric] = value
                assessed = assess_research_evidence([row], [walk], manifest=manifest)
                self.assertNotEqual(assessed["status"], "research_candidate")
                self.assertTrue(assessed["blockers"])


if __name__ == "__main__":
    unittest.main()
