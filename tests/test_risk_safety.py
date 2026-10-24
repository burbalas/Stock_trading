import unittest
from dataclasses import replace
from datetime import date

from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.models import Order, Position, Side, Signal
from trading_bot.portfolio import PortfolioManager
from trading_bot.risk import RiskManager


DAY = date(2026, 7, 9)


class RiskSafetyTests(unittest.TestCase):
    def test_config_rejects_nonfinite_numbers(self):
        fields = (
            "starting_cash", "max_position_pct", "max_order_pct", "min_trade_value",
            "target_cash_pct", "stop_loss_pct", "trailing_stop_pct", "slippage_bps",
            "commission_per_trade", "max_drawdown_stop_pct", "min_confidence",
        )
        for field in fields:
            for value in (float("nan"), float("inf"), -float("inf"), True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(BotConfig(), **{field: value}).validate()

    def test_config_rejects_fractional_counts_and_unsupported_shorts(self):
        for field in ("max_daily_orders", "max_open_positions", "short_window", "long_window", "buy_cooldown_days"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(BotConfig(), **{field: 1.5}).validate()
        with self.assertRaisesRegex(ValueError, "Short selling"):
            BotConfig(allow_short_selling=True).validate()
        with self.assertRaises(ValueError):
            BotConfig(slippage_bps=10_000).validate()
        with self.assertRaises(ValueError):
            BotConfig(symbols=["AAPL", "aapl"]).validate()

    def test_invalid_order_and_signal_inputs_fail_before_submission(self):
        for price in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(price=price), self.assertRaises(ValueError):
                Order("AAPL", Side.BUY, 1, price, "test")
        for quantity in (0, -1, 0.5, True):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                Order("AAPL", Side.BUY, quantity, 100, "test")
        for confidence in (float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                Signal("AAPL", Side.BUY, confidence, "test")
        with self.assertRaises(ValueError):
            Order("AAPL", Side.HOLD, 1, 100, "test")

    def test_sizing_reserves_slippage_and_commission(self):
        config = BotConfig(
            symbols=["AAPL"], starting_cash=1000, max_order_pct=1, max_position_pct=1,
            target_cash_pct=0, slippage_bps=1000, commission_per_trade=10,
        )
        broker = PaperBroker(1000, slippage_bps=1000, commission_per_trade=10)
        plan = PortfolioManager(config).build_order_plan(
            [Signal("AAPL", Side.BUY, 0.9, "test")], {"AAPL": 100}, broker, 1000,
        )
        self.assertEqual(len(plan.orders), 1)
        self.assertEqual(plan.orders[0].quantity, 9)
        self.assertIsNotNone(broker.submit(plan.orders[0], DAY))
        self.assertAlmostEqual(broker.cash, 0)

    def test_multiple_orders_preserve_cash_reserve_after_costs(self):
        config = BotConfig(
            symbols=["AAPL", "MSFT"], starting_cash=1000, max_order_pct=0.5,
            max_position_pct=0.5, target_cash_pct=0.1, slippage_bps=100, commission_per_trade=7,
        )
        broker = PaperBroker(1000, slippage_bps=100, commission_per_trade=7)
        prices = {"AAPL": 100, "MSFT": 100}
        plan = PortfolioManager(config).build_order_plan(
            [Signal(symbol, Side.BUY, 0.9, "test") for symbol in config.symbols], prices, broker, 1000,
        )
        self.assertEqual(len(plan.orders), 2)
        for order in plan.orders:
            self.assertIsNotNone(broker.submit(order, DAY))
        self.assertGreaterEqual(broker.cash, 100)

    def test_rounded_order_must_still_meet_minimum_notional(self):
        config = BotConfig(
            symbols=["AAPL"], starting_cash=1000, max_order_pct=0.025,
            max_position_pct=1, min_trade_value=25, slippage_bps=0,
        )
        plan = PortfolioManager(config).build_order_plan(
            [Signal("AAPL", Side.BUY, 0.9, "test")], {"AAPL": 13}, PaperBroker(1000), 1000,
        )
        self.assertEqual(plan.orders, [])
        self.assertIn("rounded order value below minimum", plan.rejected[0])

    def test_duplicate_signals_cannot_multiply_position_budget(self):
        config = BotConfig(symbols=["AAPL"], max_position_pct=0.1, slippage_bps=0)
        plan = PortfolioManager(config).build_order_plan(
            [Signal("aapl", Side.BUY, 0.8, "first"), Signal("AAPL", Side.BUY, 0.9, "second")],
            {"AAPL": 100}, PaperBroker(10_000), 10_000,
        )
        self.assertEqual(len(plan.orders), 1)
        self.assertEqual(plan.orders[0].quantity, 10)
        self.assertEqual(plan.orders[0].reason, "second")

    def test_conflicting_signals_prioritize_exit_even_with_position_adds(self):
        config = BotConfig(symbols=["AAPL"], allow_position_adds=True)
        broker = PaperBroker(10_000)
        broker.submit(Order("AAPL", Side.BUY, 1, 100, "seed"), DAY)
        plan = PortfolioManager(config).build_order_plan(
            [Signal("AAPL", Side.BUY, 0.99, "buy"), Signal("AAPL", Side.SELL, 0.7, "sell")],
            {"AAPL": 100}, broker, 10_000,
        )
        self.assertEqual([order.side for order in plan.orders], [Side.SELL])

    def test_entry_limit_does_not_suppress_exits(self):
        config = BotConfig(symbols=["AAPL", "MSFT", "NVDA"], max_daily_orders=1)
        broker = PaperBroker(10_000)
        for symbol in ("AAPL", "MSFT"):
            broker.submit(Order(symbol, Side.BUY, 1, 100, "seed"), DAY)
        plan = PortfolioManager(config).build_order_plan(
            [Signal("AAPL", Side.SELL, 0.9, "exit"), Signal("MSFT", Side.SELL, 0.9, "exit"),
             Signal("NVDA", Side.BUY, 0.9, "entry")],
            {symbol: 100 for symbol in config.symbols}, broker, 10_000,
        )
        self.assertEqual([order.side for order in plan.orders], [Side.SELL, Side.SELL, Side.BUY])

    def test_entries_fail_closed_for_invalid_account_and_prices(self):
        manager = PortfolioManager(BotConfig(symbols=["AAPL"]))
        signal = Signal("AAPL", Side.BUY, 0.9, "test")
        broker = PaperBroker(10_000)
        for price in (float("nan"), float("inf"), 0, -1):
            with self.subTest(price=price):
                self.assertEqual(manager.build_order_plan([signal], {"AAPL": price}, broker, 10_000).orders, [])
        for equity in (float("nan"), float("inf"), 0, -1):
            with self.subTest(equity=equity):
                self.assertEqual(manager.build_order_plan([signal], {"AAPL": 100}, broker, equity).orders, [])
        broker.cash = -1
        self.assertEqual(manager.build_order_plan([signal], {"AAPL": 100}, broker, 10_000).orders, [])

    def test_missing_held_mark_blocks_new_risk(self):
        broker = PaperBroker(10_000)
        broker.submit(Order("MSFT", Side.BUY, 1, 100, "seed"), DAY)
        plan = PortfolioManager(BotConfig(symbols=["AAPL"])).build_order_plan(
            [Signal("AAPL", Side.BUY, 0.9, "test")], {"AAPL": 100}, broker, 10_000,
        )
        self.assertEqual(plan.orders, [])
        self.assertIn("held-position price", plan.rejected[0])

    def test_unconfigured_symbol_cannot_be_bought(self):
        plan = PortfolioManager(BotConfig(symbols=["AAPL"])).build_order_plan(
            [Signal("MSFT", Side.BUY, 0.9, "test")], {"MSFT": 100}, PaperBroker(10_000), 10_000,
        )
        self.assertEqual(plan.orders, [])

    def test_legacy_risk_manager_uses_portfolio_controls(self):
        config = BotConfig(symbols=["AAPL"], allow_new_buys=False)
        order = RiskManager(config).build_order(
            Signal("AAPL", Side.BUY, 0.9, "test"), 100, PaperBroker(10_000), 10_000,
        )
        self.assertIsNone(order)

    def test_snapshot_rejects_missing_invalid_and_nonlong_holdings(self):
        broker = PaperBroker(1000)
        broker.submit(Order("AAPL", Side.BUY, 1, 100, "seed"), DAY)
        for prices in ({}, {"AAPL": 0}, {"AAPL": float("nan")}, {"AAPL": float("inf")}):
            with self.subTest(prices=prices), self.assertRaisesRegex(ValueError, "market price"):
                broker.snapshot(prices, DAY)
        for quantity in (-1, 0.5):
            broker.positions["AAPL"].quantity = quantity
            with self.subTest(quantity=quantity), self.assertRaisesRegex(ValueError, "whole-share"):
                broker.snapshot({"AAPL": 100}, DAY)

    def test_snapshot_ignores_closed_position_without_mark(self):
        broker = PaperBroker(1000)
        broker.positions["AAPL"] = Position("AAPL")
        self.assertEqual(broker.snapshot({}, DAY).equity, 1000)

    def test_sell_commission_is_charged_even_when_larger_than_proceeds(self):
        broker = PaperBroker(100, commission_per_trade=10)
        broker.submit(Order("AAPL", Side.BUY, 1, 1, "buy"), DAY)
        broker.submit(Order("AAPL", Side.SELL, 1, 1, "sell"), DAY)
        self.assertEqual(broker.cash, 80)
        self.assertEqual(broker.snapshot({}, DAY).equity, 80)

    def test_rejected_order_does_not_mutate_account(self):
        broker = PaperBroker(100, commission_per_trade=10)
        self.assertIsNone(broker.submit(Order("AAPL", Side.BUY, 1, 100, "too large"), DAY))
        self.assertEqual(broker.positions, {})
        self.assertEqual(broker.fills, [])
        self.assertEqual(broker.cash, 100)
        self.assertIsNone(broker.submit(Order("AAPL", Side.SELL, 1, 100, "unheld"), DAY))
        self.assertEqual(broker.positions, {})

    def test_sale_cannot_make_cash_negative_when_fees_are_unaffordable(self):
        broker = PaperBroker(12, commission_per_trade=10)
        broker.submit(Order("AAPL", Side.BUY, 1, 1, "seed"), DAY)
        self.assertEqual(broker.cash, 1)
        self.assertIsNone(broker.submit(Order("AAPL", Side.SELL, 1, 1, "exit"), DAY))
        self.assertEqual(broker.cash, 1)
        self.assertEqual(broker.positions["AAPL"].quantity, 1)
        self.assertEqual(len(broker.fills), 1)

    def test_sell_cannot_open_a_short_or_overfill(self):
        broker = PaperBroker(1000)
        broker.submit(Order("AAPL", Side.BUY, 2, 100, "seed"), DAY)
        fill = broker.submit(Order("AAPL", Side.SELL, 3, 100, "exit"), DAY)
        self.assertEqual(fill.quantity, 2)
        self.assertEqual(broker.positions["AAPL"].quantity, 0)
        self.assertEqual(broker.cash, 1000)

    def test_invalid_drawdown_or_trailing_high_fails_closed(self):
        manager = PortfolioManager(BotConfig(symbols=["AAPL"]))
        broker = PaperBroker(1000)
        broker.submit(Order("AAPL", Side.BUY, 1, 100, "seed"), DAY)
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manager.account_drawdown_orders({"AAPL": 100}, broker, value)
        with self.assertRaises(ValueError):
            manager.stop_loss_orders({"AAPL": 100}, broker, {"AAPL": float("nan")})


if __name__ == "__main__":
    unittest.main()
