from __future__ import annotations

from datetime import date, datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_bot.alpaca_integration import AlpacaPaperBroker
from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.data import StaticMarketDataProvider
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Order, Position, Side, Signal
from trading_bot.time_utils import MARKET_TZ


DAY = date(2026, 9, 22)


def remote_order(**changes):
    values = dict(id="entry", client_order_id="bot-2026-09-22-a-buy", symbol="A", side="buy",
                  qty="10", filled_qty="0", filled_avg_price=None, status="accepted",
                  submitted_at=datetime(2026, 9, 22, 10, tzinfo=MARKET_TZ), filled_at=None)
    values.update(changes)
    return SimpleNamespace(**values)


def mock_alpaca(open_orders, history=()):
    broker = AlpacaPaperBroker.__new__(AlpacaPaperBroker)
    broker._client = Mock()
    broker.open_orders = Mock(return_value=list(open_orders))
    broker.closed_orders = Mock(return_value=list(history))
    broker.refresh = Mock()
    return broker


class CancelEntryTests(unittest.TestCase):
    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_only_owned_buys_cancel_and_explicit_terminal_state_is_required(self, _sleep):
        entry = remote_order()
        foreign = remote_order(id="manual", client_order_id="manual-entry")
        protective = remote_order(id="stop", client_order_id="bot-protect-a", side="sell")
        pending = remote_order(status="pending_cancel")
        canceled = remote_order(status="canceled", filled_qty="3", filled_avg_price="10")
        broker = mock_alpaca([entry, foreign, protective])
        broker.open_orders.side_effect = [[entry, foreign, protective], [foreign, protective]]
        broker._client.get_order_by_client_id.side_effect = [pending, canceled]
        self.assertEqual(broker.cancel_open_buy_orders(set()), [canceled])
        broker._client.cancel_order_by_id.assert_called_once_with("entry")
        self.assertEqual(broker._client.get_order_by_client_id.call_count, 2)
        broker.refresh.assert_called_once()

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_cancel_error_can_be_a_fill_race(self, _sleep):
        entry = remote_order()
        filled = remote_order(status="filled", filled_qty="10", filled_avg_price="10")
        broker = mock_alpaca([entry])
        broker.open_orders.side_effect = [[entry], []]
        broker._client.cancel_order_by_id.side_effect = RuntimeError("already filled")
        broker._client.get_order_by_client_id.return_value = filled
        self.assertEqual(broker.cancel_open_buy_orders(set()), [filled])

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_disappearing_open_order_is_not_cancellation_confirmation(self, _sleep):
        broker = mock_alpaca([remote_order()])
        broker._client.get_order_by_client_id.return_value = remote_order(status="pending_cancel")
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            broker.cancel_open_buy_orders(set())
        broker.refresh.assert_not_called()

    def test_replacement_of_bot_entry_is_owned_but_other_manual_buys_are_not(self):
        ancestor = remote_order(status="replaced", replaced_by="replacement")
        replacement = remote_order(id="replacement", client_order_id="broker-generated", replaces="entry")
        broker = mock_alpaca([replacement], [ancestor])
        broker.open_orders.side_effect = [[replacement], []]
        broker._client.get_order_by_client_id.return_value = remote_order(
            id="replacement", client_order_id="broker-generated", status="canceled", replaces="entry")
        broker.cancel_open_buy_orders(set())
        broker._client.cancel_order_by_id.assert_called_once_with("replacement")

    def test_pending_intent_missing_from_open_list_is_still_canceled(self):
        entry = remote_order(client_order_id="tracked-custom-entry")
        broker = mock_alpaca([])
        broker._client.get_order_by_client_id.side_effect = [entry, remote_order(
            client_order_id="tracked-custom-entry", status="canceled")]
        broker.cancel_open_buy_orders({"tracked-custom-entry"})
        broker._client.cancel_order_by_id.assert_called_once_with("entry")

    def test_unknown_pending_intent_fails_closed(self):
        broker = mock_alpaca([])
        broker.order_by_client_id = Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "absent from broker"):
            broker.cancel_open_buy_orders({"bot-missing"})
        broker._client.cancel_order_by_id.assert_not_called()

    def test_replacement_during_cancel_requires_new_reconciliation(self):
        broker = mock_alpaca([remote_order()])
        broker._client.get_order_by_client_id.return_value = remote_order(status="replaced")
        with self.assertRaisesRegex(RuntimeError, "successor"):
            broker.cancel_open_buy_orders(set())

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_new_owned_entry_appearing_during_cancel_blocks_liquidation(self, _sleep):
        entry = remote_order()
        unexpected = remote_order(id="new-entry", client_order_id="bot-2026-09-22-b-buy", symbol="B")
        broker = mock_alpaca([entry])
        broker.open_orders.side_effect = [[entry], *([[unexpected]] * 10)]
        broker._client.get_order_by_client_id.return_value = remote_order(status="canceled")
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            broker.cancel_open_buy_orders(set())
        broker.refresh.assert_not_called()


class BuyStrategy:
    def generate(self, symbol, history):
        return Signal(symbol, Side.BUY, .95, "entry")


class PassAdvisor:
    def review(self, signal, history):
        return signal


def engine(broker, journal):
    data = StaticMarketDataProvider({"A": [Candle(DAY, "A", 10, 10, 10, 10, 1000)]})
    return LiveTradingEngine(BotConfig(symbols=["A"], starting_cash=1000, max_drawdown_stop_pct=.15),
                             data, BuyStrategy(), PassAdvisor(), broker, journal)


class HaltCycleTests(unittest.TestCase):
    def test_cancel_race_is_synced_and_newly_filled_shares_are_liquidated(self):
        for cycle in ("run_once", "run_risk_once"):
            for status, quantity in (("filled", 10), ("canceled", 3), ("canceled", 0)):
                with self.subTest(cycle=cycle, status=status, quantity=quantity), TemporaryDirectory() as directory:
                    journal = TradeJournal(directory)
                    journal.update_peak_equity(1000)
                    entry = Order("A", Side.BUY, 10, 10, "entry", "bot-2026-09-22-a-buy")
                    journal.reserve_order(entry, DAY)
                    terminal = remote_order(status=status, filled_qty=str(quantity),
                                            filled_avg_price="10" if quantity else None)
                    broker = PaperBroker(840)
                    broker.position_prices = {}
                    broker.refresh = Mock()
                    broker.closed_orders = lambda: [terminal]
                    broker.open_orders = lambda: []

                    def cancel(intents):
                        self.assertEqual(intents, {entry.client_order_id})
                        broker.cash -= quantity * 10
                        broker.positions = {"A": Position("A", quantity, 10)} if quantity else {}
                        broker.position_prices = {"A": 10} if quantity else {}
                        return [terminal]

                    broker.cancel_open_buy_orders = Mock(side_effect=cancel)
                    broker.submit = Mock(wraps=broker.submit)
                    result = getattr(engine(broker, journal), cycle)(submit_orders=True)
                    broker.cancel_open_buy_orders.assert_called_once()
                    if quantity:
                        self.assertEqual(broker.submit.call_args.args[0].quantity, quantity)
                        self.assertEqual(broker.submit.call_args.args[0].reason, "account drawdown stop")
                        self.assertEqual(result.decisions[0].fill.quantity, quantity)
                    else:
                        broker.submit.assert_not_called()
                    self.assertEqual(journal.expected_positions_from_fills(), {})
                    self.assertEqual(journal._read_state()["order_intents"][entry.client_order_id]["status"], status)

    def test_preview_and_unhalted_cycles_do_not_cancel_orders(self):
        for cycle in ("run_once", "run_risk_once"):
            for submit, cash in ((False, 840), (True, 1000)):
                with self.subTest(cycle=cycle, submit=submit), TemporaryDirectory() as directory:
                    journal = TradeJournal(directory)
                    journal.update_peak_equity(1000)
                    broker = PaperBroker(cash)
                    broker.cancel_open_buy_orders = Mock()
                    getattr(engine(broker, journal), cycle)(submit_orders=submit)
                    broker.cancel_open_buy_orders.assert_not_called()

    def test_unconfirmed_cancel_blocks_exits_and_preserves_protection(self):
        for cycle in ("run_once", "run_risk_once"):
            with self.subTest(cycle=cycle), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                journal.update_peak_equity(1000)
                journal.mark_protective_order("A", "bot-protect-a", 9, 3)
                broker = PaperBroker(810)
                broker.positions = {"A": Position("A", 3, 10)}
                broker.position_prices = {"A": 10}
                broker.cancel_open_buy_orders = Mock(side_effect=RuntimeError("not confirmed"))
                broker.cancel_open_sell_orders = Mock()
                broker.submit = Mock()
                with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                    getattr(engine(broker, journal), cycle)(submit_orders=True)
                broker.cancel_open_sell_orders.assert_not_called()
                broker.submit.assert_not_called()
                self.assertIn("A", journal._read_state()["protective_orders"])

    def test_persisted_halt_still_cancels_after_equity_rebounds(self):
        for cycle in ("run_once", "run_risk_once"):
            with self.subTest(cycle=cycle), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                journal.update_drawdown_state(1000, .15)
                journal.update_drawdown_state(800, .15)
                broker = PaperBroker(1100)
                broker.cancel_open_buy_orders = Mock(return_value=[])
                broker.refresh = Mock()
                getattr(engine(broker, TradeJournal(directory)), cycle)(submit_orders=True)
                broker.cancel_open_buy_orders.assert_called_once()
                self.assertFalse(broker.fills)

    def test_fractional_partial_is_canceled_and_synced_before_refusing_unsupported_exit(self):
        for cycle in ("run_once", "run_risk_once"):
            with self.subTest(cycle=cycle), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                journal.update_peak_equity(1000)
                terminal = remote_order(status="canceled", filled_qty="0.5", filled_avg_price="10")
                broker = PaperBroker(835)
                # Alpaca snapshots use account equity and support fractional
                # broker holdings; the in-memory simulator intentionally does not.
                broker.snapshot = lambda *args: SimpleNamespace(equity=840, cash=835, positions_value=5)
                broker.positions = {"A": Position("A", .5, 10)}
                broker.position_prices = {"A": 10}
                broker.closed_orders = lambda: [terminal]
                broker.open_orders = lambda: []
                broker.refresh = Mock()
                broker.cancel_open_buy_orders = Mock(return_value=[terminal])
                broker.submit = Mock()
                with self.assertRaisesRegex(RuntimeError, "Whole-share long-only"):
                    getattr(engine(broker, journal), cycle)(submit_orders=True)
                broker.cancel_open_buy_orders.assert_called_once()
                broker.submit.assert_not_called()
                self.assertEqual(journal.expected_positions_from_fills(), {"A": .5})


if __name__ == "__main__":
    unittest.main()
