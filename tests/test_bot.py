import unittest
from datetime import date, datetime
from tempfile import TemporaryDirectory
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch


from trading_bot.advisor import HeuristicAdvisor
from trading_bot.broker import PaperBroker
from trading_bot.cli import (
    _active_open_orders,
    _apply_profile,
    _benchmark_return,
    _health_scorecard,
    _minutes_since_regular_open,
    _namespace_with_output,
    _open_buffer_remaining_seconds,
    _position_risk_rows,
    _promotion_decision,
    _parse_float_grid,
    _reason_ingredients,
    _report_date,
    _risk_research_score,
    _risk_promotion_decision,
    _session_sleep_seconds,
    _session_retry,
    _shadow_session_args,
    _session_wait_for_open_seconds,
    _should_defer_signal_cycle_for_open_buffer,
    _sync_position_highs_from_positions,
    _strategy_reason_groups,
    _sync_alpaca_protective_fills,
    _strategy_trade_rows,
    _strategy_trade_summary,
    _validate_exec_smoke_args,
    _write_risk_research_csv,
    _write_walk_forward_csv,
)
from trading_bot.config import BotConfig
from trading_bot.data import DemoMarketDataProvider, StaticMarketDataProvider
from trading_bot.engine import TradingEngine
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Fill, Order, Position, Signal, Side
from trading_bot.portfolio import PortfolioManager
from trading_bot.reconcile import reconcile_positions
from trading_bot.research import build_expanded_strategy_specs, build_strategy_specs, evaluate_specs
from trading_bot.strategies import MovingAverageCrossStrategy, RegimeRelativeStrengthStrategy
from trading_bot.time_utils import MARKET_TZ


class BotTests(unittest.TestCase):
    def test_demo_engine_runs(self) -> None:
        config = BotConfig(symbols=["AAPL"], starting_cash=5_000, short_window=3, long_window=8)
        engine = TradingEngine(
            config=config,
            data_provider=DemoMarketDataProvider(days=60),
            strategy=MovingAverageCrossStrategy(config.short_window, config.long_window),
            advisor=HeuristicAdvisor(),
        )

        result = engine.run()

        self.assertTrue(result.snapshots)
        self.assertGreater(result.final_equity, 0)

    def test_config_rejects_bad_windows(self) -> None:
        config = BotConfig(short_window=10, long_window=10)

        with self.assertRaisesRegex(ValueError, "short_window"):
            config.validate()

    def test_config_rejects_bad_trailing_stop(self) -> None:
        config = BotConfig(trailing_stop_pct=1.0)

        with self.assertRaisesRegex(ValueError, "trailing_stop_pct"):
            config.validate()

    def test_strategy_outputs_signal_only(self) -> None:
        config = BotConfig(symbols=["AAPL"], short_window=3, long_window=8)
        rows = DemoMarketDataProvider(days=60).load(config.symbols)["AAPL"]
        strategy = MovingAverageCrossStrategy(config.short_window, config.long_window)

        signal = strategy.generate("AAPL", rows)

        self.assertIsInstance(signal, Signal)
        self.assertFalse(hasattr(signal, "quantity"))

    def test_portfolio_manager_converts_signals_to_orders(self) -> None:
        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, min_confidence=0.5)
        broker = PaperBroker(config.starting_cash)
        manager = PortfolioManager(config)

        plan = manager.build_order_plan(
            signals=[Signal("AAPL", Side.BUY, 0.9, "test signal")],
            prices={"AAPL": 100.0},
            broker=broker,
            equity=10_000,
        )

        self.assertEqual(len(plan.orders), 1)
        self.assertEqual(plan.orders[0].symbol, "AAPL")
        self.assertGreater(plan.orders[0].quantity, 0)

    def test_portfolio_manager_blocks_recent_exit_reentry(self) -> None:
        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, min_confidence=0.5, buy_cooldown_days=3)
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        broker.submit(Order("AAPL", Side.SELL, 10, 99.0, "exit"), date(2026, 7, 8))
        manager = PortfolioManager(config)
        signal = [Signal("AAPL", Side.BUY, 0.9, "buy signal")]

        blocked = manager.build_order_plan(signal, {"AAPL": 100.0}, broker, 10_000, as_of_date=date(2026, 7, 9))
        allowed = manager.build_order_plan(signal, {"AAPL": 100.0}, broker, 10_000, as_of_date=date(2026, 7, 12))

        self.assertEqual(blocked.orders, [])
        self.assertIn("buy cooldown", blocked.rejected[0])
        self.assertEqual(len(allowed.orders), 1)

    def test_portfolio_manager_sell_only_blocks_buys_not_sells(self) -> None:
        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, min_confidence=0.5, allow_new_buys=False)
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        manager = PortfolioManager(config)

        plan = manager.build_order_plan(
            signals=[
                Signal("AAPL", Side.BUY, 0.9, "buy signal"),
                Signal("AAPL", Side.SELL, 0.9, "sell signal"),
            ],
            prices={"AAPL": 101.0},
            broker=broker,
            equity=10_000,
        )

        self.assertEqual(len(plan.orders), 1)
        self.assertEqual(plan.orders[0].side, Side.SELL)

    def test_portfolio_manager_does_not_add_to_existing_position_by_default(self) -> None:
        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, min_confidence=0.5)
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        manager = PortfolioManager(config)

        plan = manager.build_order_plan(
            signals=[Signal("AAPL", Side.BUY, 0.9, "buy signal")],
            prices={"AAPL": 101.0},
            broker=broker,
            equity=10_000,
        )

        self.assertEqual(plan.orders, [])
        self.assertIn("AAPL: already held", plan.rejected)

    def test_portfolio_manager_can_add_to_position_when_enabled(self) -> None:
        config = BotConfig(
            symbols=["AAPL"],
            starting_cash=10_000,
            min_confidence=0.5,
            allow_position_adds=True,
        )
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        manager = PortfolioManager(config)

        plan = manager.build_order_plan(
            signals=[Signal("AAPL", Side.BUY, 0.9, "buy signal")],
            prices={"AAPL": 101.0},
            broker=broker,
            equity=10_000,
        )

        self.assertEqual(len(plan.orders), 1)
        self.assertEqual(plan.orders[0].symbol, "AAPL")

    def test_portfolio_manager_creates_trailing_stop_order(self) -> None:
        config = BotConfig(
            symbols=["AAPL"],
            starting_cash=10_000,
            stop_loss_pct=0.08,
            trailing_stop_pct=0.05,
        )
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        manager = PortfolioManager(config)

        orders = manager.stop_loss_orders(
            prices={"AAPL": 104.0},
            broker=broker,
            position_highs={"AAPL": 110.0},
        )

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, Side.SELL)
        self.assertEqual(orders[0].reason, "trailing stop")

    def test_account_drawdown_stop_liquidates_all_positions(self) -> None:
        config = BotConfig(
            symbols=["AAPL", "MSFT", "NVDA"],
            starting_cash=10_000,
            max_daily_orders=1,
            max_drawdown_stop_pct=0.10,
        )
        broker = PaperBroker(config.starting_cash)
        for symbol in config.symbols:
            broker.submit(Order(symbol, Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        manager = PortfolioManager(config)

        orders = manager.account_drawdown_orders(
            prices={symbol: 80.0 for symbol in config.symbols},
            broker=broker,
            account_drawdown=0.11,
        )

        self.assertEqual(len(orders), 3)
        self.assertTrue(all(order.reason == "account drawdown stop" for order in orders))

    def test_bar_stop_uses_stop_price_and_gap_open(self) -> None:
        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, stop_loss_pct=0.10)
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 7))
        manager = PortfolioManager(config)

        intraday = manager.bar_stop_orders(
            {"AAPL": Candle(date(2026, 7, 8), "AAPL", 95.0, 96.0, 89.0, 94.0, 1000)},
            broker,
            {},
        )
        gap = manager.bar_stop_orders(
            {"AAPL": Candle(date(2026, 7, 9), "AAPL", 85.0, 88.0, 84.0, 86.0, 1000)},
            broker,
            {},
        )

        self.assertEqual(intraday[0].price, 90.0)
        self.assertEqual(gap[0].price, 85.0)

    def test_live_risk_once_can_emit_trailing_stop_without_signal_scan(self) -> None:
        class StaticProvider:
            def load(self, symbols):
                return {
                    symbol: [
                        Candle(date(2026, 7, 9), symbol, 104.0, 104.0, 104.0, 104.0, 1000)
                    ]
                    for symbol in symbols
                }

        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, trailing_stop_pct=0.05)
        broker = PaperBroker(config.starting_cash)
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 8))
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_position_highs({"AAPL": 110.0}, {"AAPL"})
            engine = LiveTradingEngine(
                config=config,
                data_provider=StaticProvider(),
                strategy=MovingAverageCrossStrategy(3, 8),
                advisor=HeuristicAdvisor(),
                broker=broker,
                journal=journal,
            )

            result = engine.run_risk_once(submit_orders=False)

        self.assertEqual(len(result.decisions), 1)
        self.assertEqual(result.decisions[0].signal.side, Side.SELL)
        self.assertEqual(result.decisions[0].order.reason, "trailing stop")

    def test_live_strategy_sell_can_cancel_existing_protective_order(self) -> None:
        class StaticProvider:
            def load(self, symbols):
                return {
                    "AAPL": [
                        Candle(date(2026, 7, 1), "AAPL", 100.0, 100.0, 100.0, 100.0, 1000),
                        Candle(date(2026, 7, 2), "AAPL", 101.0, 101.0, 101.0, 101.0, 1000),
                    ]
                }

        class SellStrategy:
            def generate(self, symbol, history):
                return Signal(symbol, Side.SELL, 0.95, "strategy exit")

        class BrokerWithOpenStop(PaperBroker):
            account_id = "test"

            def __init__(self):
                super().__init__(10_000)
                self.canceled = 0

            def open_client_order_ids(self, symbols=None):
                return set()

            def open_orders(self, symbols=None):
                return [SimpleNamespace(symbol="AAPL", side=SimpleNamespace(value="sell"))]

            def cancel_open_sell_orders(self, symbol):
                self.canceled += 1
                return 1

        config = BotConfig(symbols=["AAPL"], starting_cash=10_000, min_confidence=0.5)
        broker = BrokerWithOpenStop()
        broker.submit(Order("AAPL", Side.BUY, 10, 100.0, "seed"), date(2026, 7, 1))
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.mark_protective_order("AAPL", "bot-protect-aapl-2026-07-09", 95.0, 10)
            engine = LiveTradingEngine(
                config=config,
                data_provider=StaticProvider(),
                strategy=SellStrategy(),
                advisor=HeuristicAdvisor(),
                broker=broker,
                journal=journal,
            )

            result = engine.run_once(submit_orders=True)
            protective_orders = journal._read_state().get("protective_orders")

        self.assertEqual(broker.canceled, 1)
        self.assertEqual(result.decisions[0].fill.side, Side.SELL)
        self.assertEqual(protective_orders, {})

    def test_research_evaluator_ranks_specs(self) -> None:
        symbols = ["AAPL", "MSFT"]
        data = DemoMarketDataProvider(days=120).load(symbols)
        config = BotConfig(symbols=symbols, starting_cash=10_000, short_window=5, long_window=20)

        rows = evaluate_specs(data, symbols, config, specs=build_strategy_specs()[:2], train_pct=0.6)

        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[0].test.return_pct, float)

    def test_backtest_executes_signal_on_next_open(self) -> None:
        class BuyStrategy:
            def generate(self, symbol, history):
                return Signal(symbol, Side.BUY, 0.9, "test buy")

        rows = [
            Candle(date(2026, 7, 7), "AAPL", 100.0, 101.0, 99.0, 100.0, 1000),
            Candle(date(2026, 7, 8), "AAPL", 120.0, 121.0, 119.0, 120.0, 1000),
        ]
        config = BotConfig(
            symbols=["AAPL"],
            starting_cash=10_000,
            min_confidence=0.5,
            slippage_bps=0,
        )
        engine = TradingEngine(
            config=config,
            data_provider=StaticMarketDataProvider({"AAPL": rows}),
            strategy=BuyStrategy(),
            advisor=HeuristicAdvisor(),
        )

        result = engine.run()

        self.assertEqual(result.fills[0].date, date(2026, 7, 8))
        self.assertEqual(result.fills[0].price, 120.0)

    def test_regime_relative_strength_selects_leaders(self) -> None:
        histories = {}
        for symbol, daily_gain in {"A": 0.004, "B": 0.003, "C": 0.002, "D": 0.001, "E": -0.001}.items():
            price = 100.0
            rows = []
            for day in range(80):
                price *= 1 + daily_gain
                candle_date = date.fromordinal(date(2026, 1, 1).toordinal() + day)
                rows.append(Candle(candle_date, symbol, price, price, price, price, 1000))
            histories[symbol] = rows
        strategy = RegimeRelativeStrengthStrategy(
            momentum_window=30,
            trend_window=40,
            volatility_window=10,
            skip_recent=2,
            top_n=2,
            min_breadth=0.60,
        )

        signals = strategy.generate_many(histories)
        buys = [signal.symbol for signal in signals if signal.side is Side.BUY]

        self.assertEqual(buys, ["A", "B"])

    def test_expanded_strategy_specs_are_unique_and_larger(self) -> None:
        default_specs = build_strategy_specs()
        expanded_specs = build_expanded_strategy_specs()
        names = [spec.name for spec in expanded_specs]

        self.assertGreater(len(expanded_specs), len(default_specs))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(any(name.startswith("ensemble-5-20-c") for name in names))

    def test_journal_tracks_duplicate_order_keys(self) -> None:
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            key = "2026-06-16:AAPL:buy"

            self.assertFalse(journal.has_submitted(key))
            journal.mark_submitted(key)

            self.assertTrue(journal.has_submitted(key))
            self.assertEqual(journal.submitted_count_for_date(date(2026, 6, 16)), 1)
            self.assertEqual(journal.submitted_count_for_date(date(2026, 6, 16), "buy"), 1)
            self.assertEqual(journal.submitted_count_for_date(date(2026, 6, 16), "sell"), 0)

    def test_journal_tracks_peak_drawdown(self) -> None:
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)

            peak, drawdown = journal.update_peak_equity(100_000)
            self.assertEqual(peak, 100_000)
            self.assertEqual(drawdown, 0)

            peak, drawdown = journal.update_peak_equity(90_000)
            self.assertEqual(peak, 100_000)
            self.assertAlmostEqual(drawdown, 0.10)

    def test_journal_reads_latest_exit_dates(self) -> None:
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_fill(Fill(date(2026, 7, 8), "AAPL", Side.SELL, 1, 100, 100, "exit"), "test")
            journal.record_fill(Fill(date(2026, 7, 10), "AAPL", Side.SELL, 1, 101, 101, "exit"), "test")

            self.assertEqual(journal.recent_exit_dates(), {"AAPL": date(2026, 7, 10)})

    def test_journal_tracks_position_highs(self) -> None:
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)

            highs = journal.update_position_highs({"AAPL": 100.0}, {"AAPL"})
            self.assertEqual(highs["AAPL"], 100.0)

            highs = journal.update_position_highs({"AAPL": 95.0, "MSFT": 50.0}, {"AAPL"})
            self.assertEqual(highs["AAPL"], 100.0)
            self.assertNotIn("MSFT", highs)

            highs = journal.update_position_highs({"AAPL": 105.0}, {"AAPL"})
            self.assertEqual(highs["AAPL"], 105.0)

    def test_profile_does_not_override_explicit_daily_order_limit(self) -> None:
        args = Namespace(profile="paper-operator", max_daily_orders=7)

        _apply_profile(args, {"--max-daily-orders"})

        self.assertEqual(args.max_daily_orders, 7)

    def test_paper_operator_profile_sets_promoted_risk_defaults(self) -> None:
        args = Namespace(profile="paper-operator")

        _apply_profile(args, set())

        self.assertEqual(args.stop_loss_pct, 0.10)
        self.assertEqual(args.trailing_stop_pct, 0.03)

    def test_position_risk_rows_use_active_stop(self) -> None:
        rows = _position_risk_rows(
            positions=[
                SimpleNamespace(
                    symbol="AAPL",
                    qty="10",
                    avg_entry_price="100",
                    market_value="1040",
                )
            ],
            state={"position_highs": {"AAPL": 110}},
            stop_loss_pct=0.08,
            trailing_stop_pct=0.05,
        )

        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertAlmostEqual(rows[0]["active_stop"], 104.5)
        self.assertLess(rows[0]["distance_pct"], 0)

    def test_sync_position_highs_from_positions_persists_current_prices(self) -> None:
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            positions = [
                SimpleNamespace(symbol="AAPL", qty="10", market_value="1050"),
                SimpleNamespace(symbol="MSFT", qty="0", market_value="0"),
            ]

            highs = _sync_position_highs_from_positions(journal, positions)

            self.assertEqual(highs, {"AAPL": 105.0})
            self.assertEqual(journal._read_state()["position_highs"], {"AAPL": 105.0})

    def test_minutes_since_regular_open_uses_market_time(self) -> None:
        timestamp = datetime(2026, 7, 9, 9, 37, tzinfo=MARKET_TZ)

        self.assertEqual(_minutes_since_regular_open(timestamp), 7.0)

    def test_session_sleep_seconds_caps_at_market_close(self) -> None:
        timestamp = datetime(2026, 7, 9, 15, 58, tzinfo=MARKET_TZ)
        clock = SimpleNamespace(
            timestamp=timestamp,
            next_close=datetime(2026, 7, 9, 16, 0, tzinfo=MARKET_TZ),
        )
        args = Namespace(risk_interval_minutes=5, until_close=True)

        self.assertEqual(_session_sleep_seconds(args, clock), 120.0)

    def test_session_retry_recovers_from_transient_runtime_errors(self) -> None:
        attempts = []

        def flaky_operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("temporary")
            return "ok"

        with patch("trading_bot.cli.time.sleep"):
            result = _session_retry(Namespace(max_consecutive_errors=3), "test", flaky_operation)

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)

    def test_shadow_session_args_cannot_submit_orders(self) -> None:
        args = Namespace(
            strategy="ensemble",
            shadow_strategy="relative-strength",
            journal_dir="var",
            shadow_journal_dir="var\\shadow",
            submit=True,
            sell_only=True,
            allow_closed_submit=True,
        )

        shadow = _shadow_session_args(args)

        self.assertEqual(shadow.strategy, "relative-strength")
        self.assertEqual(shadow.journal_dir, "var\\shadow")
        self.assertFalse(shadow.submit)
        self.assertFalse(shadow.sell_only)
        self.assertTrue(shadow.shadow_mode)

    def test_session_sleep_seconds_uses_interval_without_until_close(self) -> None:
        args = Namespace(risk_interval_minutes=5, until_close=False)

        self.assertEqual(_session_sleep_seconds(args), 300.0)

    def test_session_wait_for_open_includes_open_buffer_for_buys(self) -> None:
        clock = SimpleNamespace(
            timestamp=datetime(2026, 7, 9, 9, 20, tzinfo=MARKET_TZ),
            next_open=datetime(2026, 7, 9, 9, 30, tzinfo=MARKET_TZ),
        )
        args = Namespace(submit=True, sell_only=False, allow_near_open=False, open_buffer_minutes=10)

        self.assertEqual(_session_wait_for_open_seconds(args, clock), 1200.0)

    def test_signal_cycle_defers_during_open_buffer(self) -> None:
        clock = SimpleNamespace(
            is_open=True,
            timestamp=datetime(2026, 7, 9, 9, 35, tzinfo=MARKET_TZ),
        )
        args = Namespace(submit=True, sell_only=False, allow_near_open=False, open_buffer_minutes=10)

        self.assertTrue(_should_defer_signal_cycle_for_open_buffer(args, clock))
        self.assertEqual(_open_buffer_remaining_seconds(args, clock), 300.0)

    def test_signal_cycle_does_not_defer_sell_only(self) -> None:
        clock = SimpleNamespace(
            is_open=True,
            timestamp=datetime(2026, 7, 9, 9, 35, tzinfo=MARKET_TZ),
        )
        args = Namespace(submit=True, sell_only=True, allow_near_open=False, open_buffer_minutes=10)

        self.assertFalse(_should_defer_signal_cycle_for_open_buffer(args, clock))

    def test_report_date_accepts_override(self) -> None:
        args = Namespace(report_date="2026-07-24")

        self.assertEqual(_report_date(args), date(2026, 7, 24))

    def test_report_date_rejects_bad_override(self) -> None:
        args = Namespace(report_date="07/24/2026")

        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            _report_date(args)

    def test_namespace_with_output_does_not_mutate_original(self) -> None:
        args = Namespace(output="custom.md", journal_dir="var")

        updated = _namespace_with_output(args, None)

        self.assertEqual(args.output, "custom.md")
        self.assertIsNone(updated.output)
        self.assertEqual(updated.journal_dir, "var")

    def test_active_open_orders_filters_replacement_states(self) -> None:
        orders = [
            SimpleNamespace(status=SimpleNamespace(value="new")),
            SimpleNamespace(status=SimpleNamespace(value="pending_replace")),
            SimpleNamespace(status=SimpleNamespace(value="canceled")),
        ]

        self.assertEqual(_active_open_orders(orders), [orders[0]])

    def test_health_scorecard_detects_missing_duplicate_and_stale_stops(self) -> None:
        positions = [
            SimpleNamespace(symbol="AAPL", qty="10", avg_entry_price="100", market_value="1100"),
            SimpleNamespace(symbol="MSFT", qty="5", avg_entry_price="200", market_value="1050"),
        ]
        orders = [
            SimpleNamespace(symbol="AAPL", side=SimpleNamespace(value="sell"), stop_price="90"),
            SimpleNamespace(symbol="AAPL", side=SimpleNamespace(value="sell"), stop_price="91"),
        ]
        state = {"position_highs": {"AAPL": 120, "MSFT": 220}}

        health = _health_scorecard(positions, orders, state, True, 0.08, 0.05)

        self.assertEqual(health["overall"], "ATTENTION")
        self.assertEqual(health["missing_stops"], ["MSFT"])
        self.assertEqual(health["duplicate_sell_stops"], ["AAPL"])
        self.assertEqual(health["stale_stops"], ["AAPL"])

    def test_health_scorecard_ok_when_reconciled_and_protected(self) -> None:
        positions = [SimpleNamespace(symbol="AAPL", qty="10", avg_entry_price="100", market_value="1100")]
        orders = [SimpleNamespace(symbol="AAPL", side=SimpleNamespace(value="sell"), stop_price="104.5")]
        state = {"position_highs": {"AAPL": 110}}

        health = _health_scorecard(positions, orders, state, True, 0.08, 0.05)

        self.assertEqual(health["overall"], "ok")
        self.assertEqual(health["score"], health["max_score"])

    def test_exec_smoke_validation_rejects_held_symbol(self) -> None:
        args = Namespace(quantity=1, max_notional=1_000.0, stop_pct=0.03)
        broker = SimpleNamespace(positions={"AAPL": Position("AAPL", 1, 100.0)})

        with self.assertRaisesRegex(RuntimeError, "already held"):
            _validate_exec_smoke_args(args, "AAPL", 100.0, broker)

    def test_exec_smoke_validation_rejects_large_notional(self) -> None:
        args = Namespace(quantity=2, max_notional=100.0, stop_pct=0.03)
        broker = SimpleNamespace(positions={})

        with self.assertRaisesRegex(RuntimeError, "exceeds --max-notional"):
            _validate_exec_smoke_args(args, "MSFT", 75.0, broker)

    def test_exec_smoke_validation_accepts_small_unheld_symbol(self) -> None:
        args = Namespace(quantity=1, max_notional=1_000.0, stop_pct=0.03)
        broker = SimpleNamespace(positions={})

        _validate_exec_smoke_args(args, "MSFT", 500.0, broker)

    def test_strategy_trade_rows_match_closed_and_open_lots(self) -> None:
        fills = [
            Fill(date(2026, 7, 1), "AAPL", Side.BUY, 3, 100.0, 300.0, "buy"),
            Fill(date(2026, 7, 3), "AAPL", Side.SELL, 1, 110.0, 110.0, "sell"),
        ]

        rows = _strategy_trade_rows(
            fills,
            {"AAPL": 105.0},
            {
                date(2026, 7, 1): 200.0,
                date(2026, 7, 3): 210.0,
            },
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertEqual(rows[0]["quantity"], 1)
        self.assertEqual(rows[0]["pl"], 10.0)
        self.assertAlmostEqual(rows[0]["benchmark_return"], 0.05)
        self.assertAlmostEqual(rows[0]["excess_return"], 0.05)
        self.assertEqual(rows[1]["status"], "open")
        self.assertEqual(rows[1]["quantity"], 2)
        self.assertEqual(rows[1]["pl"], 10.0)

    def test_strategy_trade_summary_calculates_win_rate(self) -> None:
        rows = [
            {"status": "closed", "pl": 10.0, "pl_pct": 0.10, "benchmark_return": 0.02, "excess_return": 0.08, "holding_days": 2},
            {"status": "closed", "pl": -5.0, "pl_pct": -0.05, "benchmark_return": 0.01, "excess_return": -0.06, "holding_days": 4},
            {"status": "open", "pl": 3.0, "pl_pct": 0.03, "benchmark_return": 0.02, "excess_return": 0.01, "holding_days": 1},
        ]

        summary = _strategy_trade_summary(rows)

        self.assertEqual(summary["closed_count"], 2)
        self.assertEqual(summary["open_count"], 1)
        self.assertEqual(summary["realized_pl"], 5.0)
        self.assertEqual(summary["open_pl"], 3.0)
        self.assertEqual(summary["win_rate"], 0.5)
        self.assertEqual(summary["avg_closed_holding_days"], 3.0)
        self.assertAlmostEqual(summary["avg_trade_return"], 0.02666666666666667)
        self.assertAlmostEqual(summary["avg_excess_return"], 0.01)

    def test_reason_ingredients_splits_signal_reasons(self) -> None:
        reason = "submitted to Alpaca paper: ensemble: 20-bar momentum up; positive 5-candle momentum"

        self.assertEqual(_reason_ingredients(reason), ["20-bar momentum up", "positive 5-candle momentum"])

    def test_strategy_reason_groups_sorts_by_excess_return(self) -> None:
        rows = [
            {"reason": "submitted to Alpaca paper: ensemble: alpha; beta", "pl_pct": 0.10, "excess_return": 0.05, "pl": 10},
            {"reason": "submitted to Alpaca paper: ensemble: beta", "pl_pct": -0.02, "excess_return": -0.03, "pl": -2},
        ]

        groups = _strategy_reason_groups(rows)

        self.assertEqual(groups[0]["reason"], "alpha")
        beta = next(group for group in groups if group["reason"] == "beta")
        self.assertEqual(beta["count"], 2)

    def test_benchmark_return_uses_nearest_available_dates(self) -> None:
        prices = {
            date(2026, 7, 2): 100.0,
            date(2026, 7, 6): 110.0,
        }

        self.assertEqual(_benchmark_return(prices, date(2026, 7, 1), date(2026, 7, 7)), 0.10)

    def test_parse_float_grid_rejects_bad_values(self) -> None:
        self.assertEqual(_parse_float_grid("0.03, 0.05", "--grid"), [0.03, 0.05])
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            _parse_float_grid("0.05,1.2", "--grid")

    def test_risk_research_score_penalizes_drawdown_and_trades(self) -> None:
        self.assertLess(_risk_research_score(10.0, -20.0, 200), _risk_research_score(10.0, -5.0, 20))

    def test_write_risk_research_csv(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/risk.csv"
            rows = [
                {
                    "stop_loss_pct": 0.08,
                    "trailing_stop_pct": 0.05,
                    "return_pct": 12.3456,
                    "max_drawdown_pct": -4.0,
                    "trades": 10,
                    "score": 9.1,
                }
            ]

            _write_risk_research_csv(path, rows)

            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("stop_loss_pct,trailing_stop_pct", text)
            self.assertIn("0.0800,0.0500,12.3456", text)

    def test_write_walk_forward_csv(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/walk.csv"
            rows = [
                SimpleNamespace(
                    spec_name="ensemble-5-20",
                    folds=4,
                    positive_folds=2,
                    avg_test_return_pct=1.23456,
                    avg_benchmark_return_pct=2.0,
                    avg_edge_pct=-0.76544,
                    worst_drawdown_pct=-5.0,
                    avg_sharpe=0.5,
                    total_trades=12,
                    avg_score=1.25,
                )
            ]

            _write_walk_forward_csv(path, rows)

            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("positive_folds", text)
            self.assertIn("ensemble-5-20,4,2,1.2346", text)

    def test_promotion_decision_rejects_negative_edge(self) -> None:
        walk_rows = [
            {
                "strategy": "current",
                "positive_folds": "1",
                "avg_edge_pct": "-1",
                "worst_drawdown_pct": "-5",
                "avg_score": "1",
            },
            {
                "strategy": "candidate",
                "positive_folds": "4",
                "avg_edge_pct": "-0.5",
                "worst_drawdown_pct": "-5",
                "avg_score": "5",
            },
        ]

        decision = _promotion_decision([], walk_rows, "current", 3, 0.0, 2.0, -15.0)

        self.assertEqual(decision["recommendation"], "DO NOT PROMOTE")
        self.assertIn("average edge", decision["reason"])

    def test_promotion_decision_promotes_when_thresholds_pass(self) -> None:
        walk_rows = [
            {
                "strategy": "current",
                "positive_folds": "1",
                "avg_edge_pct": "0.1",
                "worst_drawdown_pct": "-5",
                "avg_score": "1",
            },
            {
                "strategy": "candidate",
                "positive_folds": "4",
                "avg_edge_pct": "1.5",
                "worst_drawdown_pct": "-7",
                "avg_score": "4",
            },
        ]

        decision = _promotion_decision([], walk_rows, "current", 3, 0.0, 2.0, -15.0)

        self.assertEqual(decision["recommendation"], "PROMOTE")
        self.assertEqual(decision["candidate"], "candidate")

    def test_risk_promotion_decision_rejects_small_improvement(self) -> None:
        rows = [
            {"stop_loss_pct": "0.08", "trailing_stop_pct": "0.05", "return_pct": "10", "max_drawdown_pct": "-5", "score": "6", "trades": "10"},
            {"stop_loss_pct": "0.10", "trailing_stop_pct": "0.05", "return_pct": "11", "max_drawdown_pct": "-5", "score": "7", "trades": "10"},
        ]

        decision = _risk_promotion_decision(rows, 0.08, 0.05, 2.0, 2.0, 1.0)

        self.assertEqual(decision["recommendation"], "DO NOT PROMOTE")
        self.assertIn("return improvement", decision["reason"])

    def test_risk_promotion_decision_promotes_when_thresholds_pass(self) -> None:
        rows = [
            {"stop_loss_pct": "0.08", "trailing_stop_pct": "0.05", "return_pct": "10", "max_drawdown_pct": "-5", "score": "6", "trades": "10"},
            {"stop_loss_pct": "0.10", "trailing_stop_pct": "0.05", "return_pct": "13", "max_drawdown_pct": "-5.5", "score": "9", "trades": "10"},
        ]

        decision = _risk_promotion_decision(rows, 0.08, 0.05, 2.0, 2.0, 1.0)

        self.assertEqual(decision["recommendation"], "PROMOTE")
        self.assertEqual(decision["candidate_stop_loss_pct"], 0.10)

    def test_sync_alpaca_protective_fills_records_sell_once(self) -> None:
        class FakeBroker:
            def closed_orders(self):
                return [
                    SimpleNamespace(
                        client_order_id="bot-protect-aapl-2026-07-09",
                        status=SimpleNamespace(value="filled"),
                        symbol="AAPL",
                        filled_qty="3",
                        filled_avg_price="95.50",
                        filled_at=datetime(2026, 7, 9, 14, 0, tzinfo=MARKET_TZ),
                    )
                ]

        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.mark_protective_order("AAPL", "bot-protect-aapl-2026-07-09", 95.5, 3)
            synced = _sync_alpaca_protective_fills(journal, FakeBroker())
            synced_again = _sync_alpaca_protective_fills(journal, FakeBroker())

            self.assertEqual(synced, 1)
            self.assertEqual(synced_again, 0)
            self.assertTrue(journal.has_submitted("2026-07-09:AAPL:sell"))
            self.assertTrue(journal.has_synced_order("bot-protect-aapl-2026-07-09"))
            self.assertEqual(journal._read_state().get("protective_orders"), {})

    def test_sync_alpaca_protective_fills_accepts_replacement_id(self) -> None:
        class FakeBroker:
            def closed_orders(self):
                return [
                    SimpleNamespace(
                        client_order_id="replacement-id",
                        status=SimpleNamespace(value="filled"),
                        symbol="AAPL",
                        side=SimpleNamespace(value="sell"),
                        order_type=SimpleNamespace(value="stop"),
                        filled_qty="3",
                        filled_avg_price="95.50",
                        filled_at=datetime(2026, 7, 9, 14, 0, tzinfo=MARKET_TZ),
                    )
                ]

        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.mark_protective_order("AAPL", "replacement-id", 95.5, 3)
            synced = _sync_alpaca_protective_fills(journal, FakeBroker())

            self.assertEqual(synced, 1)
            self.assertTrue(journal.has_submitted("2026-07-09:AAPL:sell"))
            self.assertTrue(journal.has_synced_order("replacement-id"))
            self.assertEqual(journal.expected_positions_from_fills(), {})

    def test_sync_alpaca_protective_fills_ignores_unknown_stop(self) -> None:
        class FakeBroker:
            def closed_orders(self):
                return [
                    SimpleNamespace(
                        client_order_id="manual-stop",
                        status=SimpleNamespace(value="filled"),
                        symbol="AAPL",
                        side=SimpleNamespace(value="sell"),
                        order_type=SimpleNamespace(value="stop"),
                        filled_qty="3",
                        filled_avg_price="95.50",
                        filled_at=datetime(2026, 7, 9, 14, 0, tzinfo=MARKET_TZ),
                    )
                ]

        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            synced = _sync_alpaca_protective_fills(journal, FakeBroker())

            self.assertEqual(synced, 0)
            self.assertFalse(journal.fills_path.exists())

    def test_reconciliation_detects_missing_position(self) -> None:
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            fill = Fill(date(2026, 7, 7), "AAPL", Side.BUY, 3, 100.0, 300.0, "test")
            journal.record_fill(fill, "test")
            journal.mark_submitted("2026-07-07:AAPL:buy")
            broker = PaperBroker(10_000)

            result = reconcile_positions(journal, broker)

            self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
