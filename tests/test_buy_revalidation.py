from __future__ import annotations

from dataclasses import replace
from datetime import date
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Fill, Order, Position, Side, Signal


DAY = date(2026, 10, 21)


class CurrentBroker(PaperBroker):
    def __init__(self):
        super().__init__(10_000)
        self.position_prices = {}
        self.refresh = Mock()
        self.open_orders = Mock(return_value=[])
        self.submit = Mock(wraps=self._submit)

    def _submit(self, order, trade_date):
        fill = super().submit(order, trade_date)
        if fill:
            self.position_prices[fill.symbol] = fill.price
        return fill


def make_engine(directory, broker, quotes, **config_changes):
    symbols = config_changes.pop("symbols", ["A"])
    config = replace(BotConfig(symbols=symbols, slippage_bps=0), **config_changes)
    provider = SimpleNamespace(
        load=lambda requested: {symbol: [Candle(DAY, symbol, 100, 101, 99, 100, 1000)]
                                for symbol in requested},
        latest_entry_prices=Mock(side_effect=quotes),
    )
    strategy = SimpleNamespace(generate=lambda symbol, history: Signal(symbol, Side.BUY, 1, "entry"))
    advisor = SimpleNamespace(review=lambda signal, history: signal)
    return LiveTradingEngine(config, provider, strategy, advisor, broker, TradeJournal(directory))


class BuyRevalidationTests(unittest.TestCase):
    def test_new_ask_reduces_quantity_and_preserves_planned_client_id(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 200}])
            result = engine.run_once(submit_orders=True)
            sent = broker.submit.call_args.args[0]
            planned = Order("A", Side.BUY, 10, 100, "entry")
            self.assertEqual((sent.quantity, sent.price), (5, 200))
            self.assertEqual(sent.client_order_id, TradeJournal.client_order_id(planned, DAY))
            self.assertEqual(result.decisions[0].order, sent)
            self.assertEqual(result.decisions[0].fill.quantity, 5)
            self.assertEqual(engine.journal._read_state()["order_intents"][sent.client_order_id]["quantity"], 5)

    def test_lower_ask_does_not_increase_original_quantity(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            result = make_engine(directory, broker, [{"A": 100}, {"A": 50}]).run_once(submit_orders=True)
            self.assertEqual((result.decisions[0].order.quantity, result.decisions[0].order.price), (10, 50))

    def test_fresh_cash_and_equity_reduce_an_earlier_plan(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()

            def refresh():
                if broker.refresh.call_count == 2:
                    broker.cash = 6_000

            broker.refresh.side_effect = refresh
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 100}],
                                 max_order_pct=1, max_position_pct=1, target_cash_pct=.5,
                                 max_drawdown_stop_pct=.9)
            result = engine.run_once(submit_orders=True)
            self.assertEqual(result.decisions[0].order.quantity, 30)
            self.assertEqual(broker.cash, 3_000)

    def test_newly_held_position_rejects_add_without_reserving(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()

            def refresh():
                if broker.refresh.call_count == 2:
                    broker.positions["A"] = Position("A", 1, 100)
                    broker.position_prices["A"] = 100
                    broker.cash = 9_900

            broker.refresh.side_effect = refresh
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 100}])
            decision = engine.run_once(submit_orders=True).decisions[0]
            self.assertIsNone(decision.order)
            self.assertIn("already held", decision.rejected_reason)
            self.assertFalse(engine.journal._read_state().get("order_intents"))
            broker.submit.assert_not_called()

    def test_sequential_buy_uses_prior_fill_and_current_position_cap(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()

            def refresh():
                broker.position_prices = {symbol: 100 for symbol in broker.positions}
                if broker.refresh.call_count == 3:
                    # A different account participant uses the last available
                    # slot after the first planned entry has filled.
                    broker.positions["C"] = Position("C", 1, 100)
                    broker.position_prices["C"] = 100
                    broker.cash -= 100

            broker.refresh.side_effect = refresh
            engine = make_engine(directory, broker,
                                 [{"A": 100}, {"B": 100}, {"A": 100}, {"B": 100}],
                                 symbols=["A", "B"], max_open_positions=2)
            decisions = engine.run_once(submit_orders=True).decisions
            broker.submit.assert_called_once()
            self.assertEqual(decisions[0].fill.symbol, "A")
            self.assertIsNone(decisions[1].order)
            self.assertIn("max open positions", decisions[1].rejected_reason)

    def test_pending_first_buy_blocks_second_buy_in_same_batch(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            broker.submit = Mock(return_value=None)
            engine = make_engine(directory, broker,
                                 [{"A": 100}, {"B": 100}, {"A": 100}, {"B": 100}], symbols=["A", "B"])
            decisions = engine.run_once(submit_orders=True).decisions
            broker.submit.assert_called_once()
            self.assertIsNotNone(decisions[0].order)
            self.assertIsNone(decisions[1].order)
            self.assertIn("unresolved buy", decisions[1].rejected_reason)
            self.assertEqual(engine.journal.submitted_count_for_date(DAY, "buy"), 1)

    def test_invalid_or_failed_second_quote_never_reserves_or_submits(self):
        for quote in ({}, {"A": float("nan")}, {"A": -1}, RuntimeError("quote is stale")):
            with self.subTest(quote=quote), TemporaryDirectory() as directory:
                broker = CurrentBroker()
                engine = make_engine(directory, broker, [{"A": 100}, quote])
                decision = engine.run_once(submit_orders=True).decisions[0]
                self.assertIsNone(decision.order)
                self.assertIn("execution revalidation", decision.rejected_reason)
                self.assertFalse(engine.journal._read_state().get("order_intents"))
                broker.submit.assert_not_called()

    def test_new_drawdown_halt_is_latched_before_buy_reservation(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()

            def refresh():
                if broker.refresh.call_count == 2:
                    broker.cash = 7_000

            broker.refresh.side_effect = refresh
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 100}])
            decision = engine.run_once(submit_orders=True).decisions[0]
            self.assertIn("drawdown stop", decision.rejected_reason)
            self.assertTrue(engine.journal._read_state()["drawdown_halted"])
            self.assertFalse(engine.journal._read_state().get("order_intents"))
            broker.submit.assert_not_called()

    def test_new_exit_cooldown_prevents_an_earlier_planned_entry(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()

            def refresh():
                if broker.refresh.call_count == 2:
                    broker.fills.append(Fill(DAY, "A", Side.SELL, 1, 100, 100, "manual exit"))

            broker.refresh.side_effect = refresh
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 100}])
            decision = engine.run_once(submit_orders=True).decisions[0]
            self.assertIn("buy cooldown", decision.rejected_reason)
            broker.submit.assert_not_called()

    def test_daily_cap_changed_since_planning_is_rechecked(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 100}], max_daily_orders=1)

            def refresh():
                if broker.refresh.call_count == 2:
                    earlier = Order("C", Side.BUY, 1, 100, "earlier")
                    engine.journal.reserve_order(earlier, DAY)
                    engine.journal.update_order_status(TradeJournal.client_order_id(earlier, DAY), "filled")

            broker.refresh.side_effect = refresh
            decision = engine.run_once(submit_orders=True).decisions[0]
            self.assertIn("max daily buy", decision.rejected_reason)
            broker.submit.assert_not_called()

    def test_preview_does_not_perform_execution_refresh_or_second_quote(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            engine = make_engine(directory, broker, [{"A": 100}])
            decision = engine.run_once(submit_orders=False).decisions[0]
            self.assertIsNotNone(decision.order)
            broker.refresh.assert_called_once()
            engine.data_provider.latest_entry_prices.assert_called_once_with(["A"])
            self.assertFalse(engine.journal._read_state().get("order_intents"))
            broker.submit.assert_not_called()

    def test_submit_failure_keeps_reserved_id_and_cannot_reissue(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            broker.submit.side_effect = TimeoutError("acceptance response was lost")
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 100}, {"A": 100}])
            with self.assertRaisesRegex(TimeoutError, "response was lost"):
                engine.run_once(submit_orders=True)
            first_id = broker.submit.call_args.args[0].client_order_id
            self.assertIn(first_id, engine.journal._read_state()["order_intents"])
            decision = engine.run_once(submit_orders=True).decisions[0]
            self.assertIsNone(decision.order)
            self.assertIn("duplicate", decision.rejected_reason)
            broker.submit.assert_called_once()

    def test_cost_reserve_and_existing_position_room_use_fresh_ask(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            broker.positions["A"] = Position("A", 20, 100)
            broker.position_prices["A"] = 100
            broker.cash = 8_000
            engine = make_engine(directory, broker, [{"A": 100}, {"A": 120}],
                                 allow_position_adds=True, max_position_pct=.25,
                                 max_order_pct=.25, slippage_bps=50, commission_per_trade=5)
            result = engine.run_once(submit_orders=True)
            sent = broker.submit.call_args.args[0]
            # Marking the existing shares at 120 makes current equity 10,400
            # and leaves only 200 of the 25% position budget available.
            self.assertEqual((sent.quantity, sent.price), (1, 120))
            self.assertEqual(result.decisions[0].order, sent)
            estimated_cost = sent.quantity * sent.price * 1.005 + 5
            self.assertLessEqual(estimated_cost, 10_400 * .25 - 20 * 120)


if __name__ == "__main__":
    unittest.main()
