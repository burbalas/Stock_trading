import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.data import StaticMarketDataProvider
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Order, Position, Side, Signal


DAY = date(2026, 9, 22)


def exit_order(quantity=10):
    order = Order("A", Side.SELL, quantity, 8, "stop loss")
    return replace(order, client_order_id=TradeJournal.client_order_id(order, DAY))


def remote(order, status="rejected", filled=0, **changes):
    values = dict(id="remote-" + order.client_order_id, client_order_id=order.client_order_id,
                  symbol=order.symbol, side=order.side.value, qty=str(order.quantity),
                  status=status, filled_qty=str(filled), filled_avg_price="8" if filled else None,
                  filled_at=datetime(2026, 9, 22, 15, tzinfo=timezone.utc) if filled else None,
                  submitted_at=datetime(2026, 9, 22, 15, tzinfo=timezone.utc))
    values.update(changes)
    return SimpleNamespace(**values)


class Hold:
    def generate(self, symbol, history):
        return Signal(symbol, Side.HOLD, 0, "hold")

    def review(self, signal, history):
        return signal


class RetryBroker(PaperBroker):
    def __init__(self, reject_forever=False):
        super().__init__(900)
        self.positions["A"] = Position("A", 10, 10)
        self.position_prices = {"A": 8}
        self.sent = []
        self.remote_orders = {}
        self.lookups = []
        self.reject_forever = reject_forever
        self.on_lookup = None

    def trading_date(self):
        return DAY

    def refresh(self):
        pass

    def order_by_client_id(self, client_id):
        self.lookups.append(client_id)
        if self.on_lookup:
            self.on_lookup()
        return self.remote_orders.get(client_id)

    def submit(self, order, trade_date):
        self.sent.append(order)
        if len(self.sent) == 1 or self.reject_forever:
            result = None
            self.last_submitted_order = remote(order)
        else:
            result = super().submit(order, trade_date)
            self.last_submitted_order = remote(order, "filled", result.quantity)
        self.remote_orders[order.client_order_id] = self.last_submitted_order
        return result


def engine(directory, broker):
    data = StaticMarketDataProvider({"A": [Candle(DAY, "A", 8, 8, 8, 8, 1000)]})
    return LiveTradingEngine(BotConfig(symbols=["A"], starting_cash=1000), data,
                             Hold(), Hold(), broker, TradeJournal(directory))


class ExitRetryTests(unittest.TestCase):
    def test_rejected_exit_retries_in_risk_and_signal_cycles(self):
        for method in ("run_risk_once", "run_once"):
            with self.subTest(method=method), TemporaryDirectory() as directory:
                broker = RetryBroker()
                first = engine(directory, broker)
                getattr(first, method)(submit_orders=True)
                restarted = engine(directory, broker)
                getattr(restarted, method)(submit_orders=True)
                self.assertEqual(len(broker.sent), 2)
                self.assertEqual(broker.positions["A"].quantity, 0)
                self.assertEqual(broker.sent[1].client_order_id, exit_order().client_order_id + "-retry-2")
                self.assertEqual(broker.lookups, [exit_order().client_order_id])
                self.assertEqual(len(restarted.journal._read_state()["order_intents"]), 2)

    def test_three_attempt_bound_survives_process_restart(self):
        with TemporaryDirectory() as directory:
            broker = RetryBroker(reject_forever=True)
            for _ in range(5):
                engine(directory, broker).run_risk_once(submit_orders=True)
            self.assertEqual(len(broker.sent), 3)
            self.assertEqual(len({order.client_order_id for order in broker.sent}), 3)
            self.assertEqual(broker.positions["A"].quantity, 10)

    def test_unknown_or_active_broker_state_cannot_retry_cached_rejection(self):
        for status in (None, "new", "partially_filled", "pending_cancel", "replaced", "filled"):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                broker = RetryBroker()
                bot = engine(directory, broker)
                bot.run_risk_once(submit_orders=True)
                client_id = broker.sent[0].client_order_id
                if status is None:
                    broker.remote_orders.clear()
                else:
                    broker.remote_orders[client_id].status = status
                bot.run_risk_once(submit_orders=True)
                self.assertEqual(len(broker.sent), 1)

    def test_retry_uses_refreshed_remaining_holding(self):
        with TemporaryDirectory() as directory:
            broker = RetryBroker()
            bot = engine(directory, broker)
            bot.run_risk_once(submit_orders=True)
            broker.on_lookup = lambda: setattr(broker.positions["A"], "quantity", 7)
            bot.run_risk_once(submit_orders=True)
            self.assertEqual([order.quantity for order in broker.sent], [10, 7])

    def test_preview_does_not_prepare_or_reserve_retry(self):
        with TemporaryDirectory() as directory:
            broker = RetryBroker()
            bot = engine(directory, broker)
            bot.run_risk_once(submit_orders=True)
            bot.run_risk_once(submit_orders=False)
            self.assertEqual(len(broker.sent), 1)
            self.assertFalse(broker.lookups)
            self.assertNotIn("risk_exit_retry_authorizations", bot.journal._read_state())

    def test_canceled_partial_execution_is_journaled_once_under_original_id(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            original = exit_order()
            self.assertTrue(journal.reserve_order(original, DAY))
            canceled = remote(original, "canceled", 3)
            first = journal.prepare_risk_exit_retry(exit_order(10), DAY, canceled)
            repeated = journal.prepare_risk_exit_retry(exit_order(10), DAY, canceled)
            self.assertEqual(first, repeated)
            self.assertEqual(first.quantity, 7)
            with journal.fills_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["broker_order_id"], canceled.id)
            self.assertEqual(rows[0]["client_order_id"], original.client_order_id)
            self.assertTrue(journal.reserve_order(first, DAY))
            self.assertFalse(journal.reserve_order(first, DAY))
            self.assertIn(original.client_order_id, journal._read_state()["order_intents"])

    def test_authorization_cannot_be_forged_or_used_for_buys_or_strategy_exits(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            original = exit_order()
            self.assertTrue(journal.reserve_order(original, DAY))
            forged = replace(original, client_order_id=original.client_order_id + "-retry-2")
            self.assertFalse(journal.reserve_order(forged, DAY))
            for candidate in (replace(original, side=Side.BUY), replace(original, reason="strategy sell")):
                self.assertIsNone(journal.prepare_risk_exit_retry(candidate, DAY, remote(original)))
            retry = journal.prepare_risk_exit_retry(original, DAY, remote(original))
            self.assertFalse(journal.reserve_order(replace(retry, quantity=11), DAY))
            self.assertFalse(journal.reserve_order(replace(retry, reason="strategy sell"), DAY))

    def test_remote_identity_mismatch_never_authorizes_or_journals(self):
        for changes in ({"symbol": "B"}, {"side": "buy"}, {"qty": "9"},
                        {"client_order_id": "unrelated"}, {"filled_qty": "11"}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                original = exit_order()
                journal.reserve_order(original, DAY)
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    journal.prepare_risk_exit_retry(original, DAY, remote(original, **changes))
                self.assertNotIn("risk_exit_retry_authorizations", journal._read_state())
                self.assertFalse(journal.fills_path.exists())

    def test_fractional_or_fully_filled_remainder_never_authorizes_retry(self):
        for filled in (0.5, 10):
            with self.subTest(filled=filled), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                original = exit_order()
                journal.reserve_order(original, DAY)
                self.assertIsNone(journal.prepare_risk_exit_retry(original, DAY, remote(original, "canceled", filled)))

    def test_previous_fill_cannot_disappear_or_change_broker_identity(self):
        for changes in ({"filled_qty": "0", "filled_avg_price": None}, {"id": "different-broker-id"}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                original = exit_order()
                journal.reserve_order(original, DAY)
                journal.record_broker_order(remote(original, "partially_filled", 3))
                with self.assertRaisesRegex(RuntimeError, "regressed"):
                    journal.prepare_risk_exit_retry(original, DAY, remote(original, "canceled", 3, **changes))
                self.assertNotIn("risk_exit_retry_authorizations", journal._read_state())


if __name__ == "__main__":
    unittest.main()
