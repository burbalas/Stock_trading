from __future__ import annotations

import csv
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading_bot.alpaca_integration import AlpacaMarketDataProvider, AlpacaPaperBroker, _trading_url_override, confirmed_order_fill
from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Fill, Order, Position, Side, Signal
from trading_bot.reconcile import reconcile_positions, sync_broker_orders
from trading_bot.time_utils import MARKET_TZ


class NotFound(Exception):
    status_code = 404


def remote_order(**changes):
    values = dict(id="remote-1", client_order_id="bot-2026-09-14-aapl-buy", symbol="AAPL",
                  side="buy", qty="10", filled_qty="0", filled_avg_price=None, status="accepted",
                  submitted_at=datetime(2026, 9, 14, 10, tzinfo=MARKET_TZ), filled_at=None)
    values.update(changes)
    return SimpleNamespace(**values)


def broker_with_client(client):
    broker = AlpacaPaperBroker.__new__(AlpacaPaperBroker)
    broker._client = client
    broker._order_side = SimpleNamespace(BUY="buy", SELL="sell")
    broker._time_in_force = SimpleNamespace(DAY="day", GTC="gtc")
    broker._query_order_status = SimpleNamespace(OPEN="open", CLOSED="closed")
    broker._get_orders_request = lambda **kwargs: SimpleNamespace(**kwargs)
    broker._market_order_request = lambda **kwargs: SimpleNamespace(**kwargs)
    broker._get_calendar_request = lambda **kwargs: SimpleNamespace(**kwargs)
    broker.positions = {"AAPL": Position("AAPL", 10, 100)}
    broker.fills = []
    broker.refresh = Mock()
    broker.last_submitted_order = None
    return broker


def quote_provider(quotes):
    provider = AlpacaMarketDataProvider.__new__(AlpacaMarketDataProvider)
    provider.feed = "iex"
    provider._latest_quote_request = lambda **kwargs: SimpleNamespace(**kwargs)
    provider._client = Mock()
    provider._client.get_stock_latest_quote.return_value = quotes
    return provider


class ExecutionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.order = Order("AAPL", Side.BUY, 10, 100, "test", "bot-2026-09-14-aapl-buy")

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_accepted_order_does_not_fabricate_a_fill(self, _sleep):
        remote = remote_order()
        client = Mock()
        client.get_order_by_client_id.side_effect = [NotFound(), remote, remote, remote, remote]
        client.submit_order.return_value = remote
        broker = broker_with_client(client)
        self.assertIsNone(broker.submit(self.order, date(2026, 9, 14)))
        self.assertEqual(broker.fills, [])
        self.assertIs(broker.last_submitted_order, remote)
        client.submit_order.assert_called_once()

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_partial_fill_preserves_actual_quantity_and_price(self, _sleep):
        remote = remote_order(status="partially_filled", filled_qty="3", filled_avg_price="101.25")
        client = Mock()
        client.get_order_by_client_id.return_value = remote
        broker = broker_with_client(client)
        fill = broker.submit(self.order)
        self.assertEqual((fill.quantity, fill.price, fill.value), (3, 101.25, 303.75))
        client.submit_order.assert_not_called()

    def test_fractional_execution_is_not_rounded_away(self):
        fill = confirmed_order_fill(remote_order(filled_qty="0.25", filled_avg_price="100.25"))
        self.assertEqual(fill.quantity, 0.25)
        self.assertEqual(fill.value, 25.0625)

    def test_submit_timeout_recovers_same_id_without_a_second_submission(self):
        remote = remote_order(status="filled", filled_qty="10", filled_avg_price="101")
        client = Mock()
        client.get_order_by_client_id.side_effect = [NotFound(), remote]
        client.submit_order.side_effect = TimeoutError("response lost after acceptance")
        fill = broker_with_client(client).submit(self.order)
        self.assertEqual(fill.quantity, 10)
        client.submit_order.assert_called_once()

    def test_lookup_failure_is_not_misinterpreted_as_missing_order(self):
        client = Mock()
        client.get_order_by_client_id.side_effect = PermissionError("unauthorized")
        with self.assertRaisesRegex(RuntimeError, "unauthorized"):
            broker_with_client(client).submit(self.order)
        client.submit_order.assert_not_called()

    def test_client_id_collision_cannot_change_order_quantity(self):
        client = Mock()
        client.get_order_by_client_id.return_value = remote_order(qty="20")
        with self.assertRaisesRegex(RuntimeError, "different order"):
            broker_with_client(client).submit(self.order)
        client.submit_order.assert_not_called()

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_pending_cancellation_prevents_replacement_sell(self, _sleep):
        remote = remote_order(side="sell", status="pending_cancel")
        client = Mock()
        client.get_orders.return_value = [remote]
        broker = broker_with_client(client)
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            broker.cancel_open_sell_orders("AAPL")
        client.submit_order.assert_not_called()

    def test_sell_rechecks_current_position_after_cancel_race(self):
        client = Mock()
        client.get_order_by_client_id.side_effect = NotFound()
        broker = broker_with_client(client)
        broker.positions = {}
        with self.assertRaisesRegex(RuntimeError, "currently confirmed long position"):
            broker.submit(Order("AAPL", Side.SELL, 10, 100, "exit", "bot-2026-09-14-aapl-sell"))
        client.submit_order.assert_not_called()

    def test_base_url_cannot_route_paper_adapter_to_real_money(self):
        for url in ("https://api.alpaca.markets/v2", "http://paper-api.alpaca.markets", "https://paper-api.alpaca.markets.evil.test"):
            with self.subTest(url=url), patch.dict(os.environ, {"ALPACA_BASE_URL": url}), patch("trading_bot.alpaca_integration._load_local_env"):
                with self.assertRaisesRegex(RuntimeError, "paper-api"):
                    _trading_url_override()
        with patch.dict(os.environ, {"ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2"}), patch("trading_bot.alpaca_integration._load_local_env"):
            self.assertEqual(_trading_url_override(), "https://paper-api.alpaca.markets")

    def test_trade_date_uses_exchange_day_after_lithuanian_midnight(self):
        broker = broker_with_client(Mock())
        broker.market_clock = lambda: SimpleNamespace(timestamp=datetime(2026, 9, 14, 18, 30, tzinfo=MARKET_TZ))
        self.assertEqual(broker.trading_date(), date(2026, 9, 14))

    def test_completed_session_respects_exchange_holiday_and_early_close(self):
        client = Mock()
        client.get_calendar.return_value = [
            SimpleNamespace(date=date(2026, 11, 25), close=datetime(2026, 11, 25, 16)),
            SimpleNamespace(date=date(2026, 11, 27), close=datetime(2026, 11, 27, 13)),
        ]
        broker = broker_with_client(client)
        broker.market_clock = lambda: SimpleNamespace(timestamp=datetime(2026, 11, 27, 12, tzinfo=MARKET_TZ))
        self.assertEqual(broker.latest_completed_session(), date(2026, 11, 25))
        broker.market_clock = lambda: SimpleNamespace(timestamp=datetime(2026, 11, 27, 14, tzinfo=MARKET_TZ))
        self.assertEqual(broker.latest_completed_session(), date(2026, 11, 27))

    def test_cumulative_partial_fills_survive_restart_without_double_counting(self):
        remote = remote_order(status="partially_filled", filled_qty="0.25", filled_avg_price="100")
        broker = SimpleNamespace(closed_orders=lambda: [], open_orders=lambda: [remote], positions={})
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            self.assertEqual(sync_broker_orders(journal, broker), 1)
            self.assertEqual(journal.expected_positions_from_fills(), {"AAPL": 0.25})
            remote.filled_qty, remote.filled_avg_price, remote.status = "10", "102", "filled"
            self.assertEqual(sync_broker_orders(TradeJournal(directory), broker), 1)
            self.assertEqual(sync_broker_orders(TradeJournal(directory), broker), 0)
            self.assertEqual(journal.expected_positions_from_fills(), {"AAPL": 10})
            with journal.fills_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(sum(float(row["value"]) for row in rows), 1020)

    def test_canceled_order_still_synchronizes_its_executed_part(self):
        remote = remote_order(status="canceled", filled_qty="3", filled_avg_price="99")
        broker = SimpleNamespace(closed_orders=lambda: [remote], positions={})
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            self.assertEqual(sync_broker_orders(journal, broker), 1)
            self.assertEqual(journal.expected_positions_from_fills(), {"AAPL": 3})

    def test_replaced_protection_recovers_without_local_mapping_and_survives_restart(self):
        entry = remote_order(id="entry", status="filled", filled_qty="10", filled_avg_price="100")
        original = remote_order(id="original", client_order_id="bot-protect-aapl-old", side="sell", status="replaced")
        middle = remote_order(id="middle", client_order_id="broker-middle", side="sell", status="replaced", replaces="original")
        final = remote_order(id="final", client_order_id="broker-final", side="sell", status="filled",
                             replaces="middle", filled_qty="10", filled_avg_price="95")
        manual = remote_order(id="manual", client_order_id="manual-stop", side="sell", status="filled",
                              filled_qty="4", filled_avg_price="95")
        # Broker history arrives newest first and may contain page-boundary duplicates.
        broker = SimpleNamespace(closed_orders=lambda: [final, manual, middle, final, original, entry],
                                 open_orders=lambda: [], positions={})
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            self.assertEqual(sync_broker_orders(journal, broker), 2)
            self.assertTrue(reconcile_positions(journal, broker).ok)
            self.assertTrue(journal.has_synced_order("broker-final"))
            self.assertEqual(sync_broker_orders(TradeJournal(directory), broker), 0)
            with journal.fills_path.open(newline="") as handle:
                self.assertEqual({row["broker_order_id"] for row in csv.DictReader(handle)}, {"entry", "final"})

    def test_replacement_partial_ancestor_requires_execution_ledger(self):
        original = remote_order(id="original", client_order_id="broker-original", side="sell", status="replaced",
                                replaced_by="middle", filled_qty="2", filled_avg_price="99")
        middle = remote_order(id="middle", client_order_id="broker-middle", side="sell", status="replaced",
                              replaced_by="final")
        final = remote_order(id="final", client_order_id="tracked-final", side="sell", status="partially_filled",
                             filled_qty="3", filled_avg_price="98")
        broker = SimpleNamespace(closed_orders=lambda: [middle, original], open_orders=lambda: [final], positions={})
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.mark_protective_order("AAPL", "tracked-final", 98, 8)
            with self.assertRaisesRegex(RuntimeError, "execution-ledger reconciliation"):
                sync_broker_orders(journal, broker)
            self.assertFalse(journal.fills_path.exists())

    def test_replacement_cycles_terminate_and_do_not_adopt_unanchored_orders(self):
        original = remote_order(id="original", client_order_id="bot-protect-aapl-old", side="sell", status="replaced",
                                replaces="final", replaced_by="final")
        final = remote_order(id="final", client_order_id="broker-final", side="sell", status="filled",
                             replaces="original", replaced_by="original", filled_qty="10", filled_avg_price="95")
        manual_a = remote_order(id="manual-a", client_order_id="manual-a", side="sell", replaces="manual-b",
                                status="filled", filled_qty="2", filled_avg_price="95")
        manual_b = remote_order(id="manual-b", client_order_id="manual-b", side="sell", replaces="manual-a")
        broker = SimpleNamespace(closed_orders=lambda: [manual_a, final, original, manual_b], positions={})
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            with self.assertRaisesRegex(RuntimeError, "execution-ledger reconciliation"):
                sync_broker_orders(journal, broker)
            self.assertFalse(journal.fills_path.exists())

    def test_replacement_chain_cannot_change_symbol_or_side(self):
        original = remote_order(id="original", client_order_id="bot-protect-aapl-old", side="sell", status="replaced")
        for changes in ({"symbol": "MSFT"}, {"side": "buy"}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                final = remote_order(id="final", client_order_id="broker-final", side="sell", status="filled",
                                     replaces="original", filled_qty="10", filled_avg_price="95")
                for attribute, value in changes.items():
                    setattr(final, attribute, value)
                journal = TradeJournal(directory)
                broker = SimpleNamespace(closed_orders=lambda: [final, original], positions={})
                with self.assertRaisesRegex(RuntimeError, "replacement chain changed"):
                    sync_broker_orders(journal, broker)
                self.assertFalse(journal.fills_path.exists())

    def test_historical_protective_fill_does_not_clear_newer_active_protection(self):
        completed = remote_order(id="completed", client_order_id="bot-protect-aapl-old", side="sell", status="filled",
                                 filled_qty="10", filled_avg_price="95")
        active = remote_order(id="active", client_order_id="tracked-new-stop", side="sell", status="accepted")
        broker = SimpleNamespace(closed_orders=lambda: [completed], open_orders=lambda: [active], positions={})
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.mark_protective_order("AAPL", active.client_order_id, 95, 10)
            self.assertEqual(sync_broker_orders(journal, broker), 1)
            self.assertEqual(journal._read_state()["protective_orders"]["AAPL"]["client_order_id"], active.client_order_id)

    def test_crash_after_csv_commit_cannot_duplicate_fill_on_recovery(self):
        remote = remote_order(status="filled", filled_qty="10", filled_avg_price="100")
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            with patch.object(journal, "mark_submitted", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    journal.record_broker_order(remote)
            restarted = TradeJournal(directory)
            self.assertIsNone(restarted.record_broker_order(remote))
            self.assertEqual(restarted.expected_positions_from_fills(), {"AAPL": 10})

    def test_legacy_fabricated_fill_mismatch_requires_reconciliation(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_fill(Fill(date(2026, 9, 14), "AAPL", Side.BUY, 10, 100, 1000, "legacy"), "alpaca-paper")
            with self.assertRaisesRegex(RuntimeError, "Legacy fills disagree"):
                journal.record_broker_order(remote_order(filled_qty="3", filled_avg_price="100"))

    def test_atomic_reservation_allows_only_one_concurrent_submit(self):
        with TemporaryDirectory() as directory:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: TradeJournal(directory).reserve_order(self.order, date(2026, 9, 14)), range(24)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(TradeJournal(directory).submitted_count_for_date(date(2026, 9, 14), "buy"), 1)

    def test_damaged_state_does_not_reset_submission_reservations(self):
        for contents in ("", "[]", "null"):
            with self.subTest(contents=contents), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                journal.state_path.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "restore or reconcile"):
                    journal.reserve_order(self.order, date(2026, 9, 14))

    def test_execution_lock_excludes_another_process(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            code = "import sys; from trading_bot.journal import TradeJournal\nwith TradeJournal(sys.argv[1]).lock('execution', timeout=0): pass"
            with journal.lock("execution", timeout=0):
                result = subprocess.run([sys.executable, "-c", code, directory], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another process", result.stderr)
            result = subprocess.run([sys.executable, "-c", code, directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_account_change_requires_explicit_archive(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.bind_account("old")
            journal.record_fill(Fill(date(2026, 9, 14), "AAPL", Side.SELL, 10, 100, 1000, "exit"), "alpaca-paper")
            with self.assertRaisesRegex(RuntimeError, "differs"):
                journal.bind_account("new")
            self.assertEqual(journal._read_state()["account_id"], "old")
            self.assertIn("AAPL", journal.recent_exit_dates())
            journal.reset_for_account("new")
            self.assertEqual(journal.recent_exit_dates(), {})
            self.assertTrue(list((journal.directory / "archive").glob("*/fills.csv")))

    def test_reconciliation_includes_unexpected_short_positions(self):
        with TemporaryDirectory() as directory:
            broker = SimpleNamespace(positions={"AAPL": Position("AAPL", -2, 100)})
            self.assertFalse(reconcile_positions(TradeJournal(directory), broker).ok)

    def test_fractional_journal_arithmetic_preserves_exact_flat_position(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            for side, quantity in ((Side.BUY, 0.1), (Side.BUY, 0.2), (Side.SELL, 0.3)):
                journal.record_fill(Fill(date(2026, 9, 14), "AAPL", side, quantity, 100, quantity * 100, "partial"), "alpaca-paper")
                journal.mark_submitted(f"2026-09-14:AAPL:{side.value}")
            self.assertEqual(journal.expected_positions_from_fills(), {})
            self.assertTrue(reconcile_positions(journal, SimpleNamespace(positions={})).ok)

    def test_cumulative_fractional_deltas_do_not_introduce_float_drift(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            remote = remote_order(filled_qty="0.1", filled_avg_price="100")
            journal.record_broker_order(remote)
            remote.filled_qty = "0.3"
            delta = journal.record_broker_order(remote)
            self.assertEqual(delta.quantity, 0.2)
            journal.record_broker_order(remote_order(id="remote-sell", client_order_id="bot-2026-09-14-aapl-sell",
                                                    side="sell", filled_qty="0.3", filled_avg_price="100"))
            self.assertEqual(journal.expected_positions_from_fills(), {})

    def test_oversold_journal_position_is_not_silently_dropped(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_fill(Fill(date(2026, 9, 14), "AAPL", Side.SELL, 3, 100, 300, "exit"), "alpaca-paper")
            journal.mark_submitted("2026-09-14:AAPL:sell")
            self.assertEqual(journal.expected_positions_from_fills(), {"AAPL": -3})
            result = reconcile_positions(journal, SimpleNamespace(positions={}))
            self.assertFalse(result.ok)
            self.assertIn("expected -3", result.messages[0])

    def make_engine(self, broker, directory, latest=date(2026, 9, 11)):
        provider = SimpleNamespace(load=lambda symbols: {symbol: [Candle(latest, symbol, 100, 101, 99, 100, 1000)] for symbol in symbols})
        strategy = SimpleNamespace(generate=lambda symbol, history: Signal(symbol, Side.BUY, 1, "entry"))
        advisor = SimpleNamespace(review=lambda signal, history: signal)
        return LiveTradingEngine(BotConfig(symbols=["AAPL"], starting_cash=10000), provider, strategy, advisor, broker, TradeJournal(directory))

    def test_live_engine_rejects_stale_daily_data_before_submitting(self):
        broker = PaperBroker(10000)
        broker.latest_completed_session = lambda: date(2026, 9, 11)
        broker.submit = Mock()
        with TemporaryDirectory() as directory:
            engine = self.make_engine(broker, directory, latest=date(2026, 9, 10))
            with self.assertRaisesRegex(RuntimeError, "stale completed daily bars"):
                engine.run_once(submit_orders=True)
        broker.submit.assert_not_called()

    def test_entry_size_uses_fresh_ask_after_overnight_gap(self):
        broker = PaperBroker(10000)
        with TemporaryDirectory() as directory:
            engine = self.make_engine(broker, directory)
            old_size = engine.run_once().decisions[0].order.quantity
            provider = quote_provider({"AAPL": SimpleNamespace(timestamp=datetime.now(timezone.utc), bid_price=199, ask_price=200)})
            provider.load = engine.data_provider.load
            engine.data_provider = provider
            engine.strategy.generate = Mock(return_value=Signal("AAPL", Side.BUY, 1, "entry"))
            order = engine.run_once().decisions[0].order
            self.assertEqual(order.price, 200)
            self.assertLess(order.quantity, old_size)
            self.assertLessEqual(order.quantity * order.price, 10000 * engine.config.max_order_pct)
            history = engine.strategy.generate.call_args.args[1]
            self.assertEqual(history[-1].close, 100)
            request = provider._client.get_stock_latest_quote.call_args.args[0]
            self.assertEqual((request.symbol_or_symbols, request.feed), (["AAPL"], "iex"))

    def test_stale_entry_quote_rejects_trade_without_daily_close_fallback(self):
        broker = PaperBroker(10000)
        broker.submit = Mock()
        with TemporaryDirectory() as directory:
            engine = self.make_engine(broker, directory)
            provider = quote_provider({"AAPL": SimpleNamespace(timestamp=datetime.now(timezone.utc) - timedelta(seconds=121), bid_price=99, ask_price=100)})
            provider.load = engine.data_provider.load
            engine.data_provider = provider
            result = engine.run_once(submit_orders=True)
            self.assertIsNone(result.decisions[0].order)
            self.assertIn("Stale or future quote", result.decisions[0].rejected_reason)
        broker.submit.assert_not_called()

    def test_invalid_entry_quote_prices_and_timestamps_are_rejected(self):
        now = datetime.now(timezone.utc)
        cases = [
            (now, 101, 100, "crossed"),
            (now, 90, 100, "spread"),
            (now, 0, 100, "Invalid"),
            (now, float("nan"), 100, "Invalid"),
            (now, 100, float("inf"), "Invalid"),
            (now.replace(tzinfo=None), 99, 100, "timezone-aware"),
            (now + timedelta(seconds=10), 99, 100, "future quote"),
        ]
        for timestamp, bid, ask, error in cases:
            with self.subTest(error=error, bid=bid, ask=ask):
                provider = quote_provider({"AAPL": SimpleNamespace(timestamp=timestamp, bid_price=bid, ask_price=ask)})
                with self.assertRaisesRegex(RuntimeError, error):
                    provider.latest_entry_prices(["AAPL"])
        with self.assertRaisesRegex(RuntimeError, "Missing latest quote"):
            quote_provider({}).latest_entry_prices(["AAPL"])

    def test_quote_lookup_is_skipped_when_only_protective_exit_is_needed(self):
        broker = PaperBroker(10000)
        broker.positions = {"AAPL": Position("AAPL", 3, 100)}
        broker.position_prices = {"AAPL": 80}
        with TemporaryDirectory() as directory:
            engine = self.make_engine(broker, directory)
            engine.data_provider.latest_entry_prices = Mock(side_effect=AssertionError("exit must use current broker mark"))
            order = engine.run_once().decisions[0].order
            self.assertEqual((order.side, order.price), (Side.SELL, 80))
            engine.data_provider.latest_entry_prices.assert_not_called()

    def test_accepted_unfilled_submission_is_reserved_and_not_reissued(self):
        broker = PaperBroker(10000)
        broker.submit = Mock(return_value=None)
        with TemporaryDirectory() as directory:
            engine = self.make_engine(broker, directory)
            first = engine.run_once(submit_orders=True)
            second = engine.run_once(submit_orders=True)
            self.assertIsNone(first.decisions[0].fill)
            self.assertIsNone(second.decisions[0].order)
        broker.submit.assert_called_once()

    def test_risk_exit_covers_holding_outside_configured_universe(self):
        broker = PaperBroker(10000)
        broker.positions = {"MSFT": Position("MSFT", 3, 100)}
        broker.position_prices = {"MSFT": 80}
        with TemporaryDirectory() as directory:
            result = self.make_engine(broker, directory).run_risk_once()
        self.assertEqual(result.decisions[0].order.symbol, "MSFT")
        self.assertEqual(result.decisions[0].order.price, 80)

    def test_triggered_trailing_exit_cancels_older_lower_broker_stop(self):
        for cycle in ("run_once", "run_risk_once"):
            with self.subTest(cycle=cycle), TemporaryDirectory() as directory:
                broker = PaperBroker(10000)
                broker.positions = {"AAPL": Position("AAPL", 3, 100)}
                broker.position_prices = {"AAPL": 104}
                old_stop = remote_order(side="sell", status="new", stop_price="95", qty="3")
                broker.open_orders = Mock(return_value=[old_stop])
                broker.cancel_open_sell_orders = Mock(side_effect=lambda symbol: broker.open_orders.configure_mock(return_value=[]))
                broker.submit = Mock(wraps=broker.submit)
                engine = self.make_engine(broker, directory)
                engine.journal.update_position_highs({"AAPL": 110}, {"AAPL"})
                result = getattr(engine, cycle)(submit_orders=True)
                broker.cancel_open_sell_orders.assert_called_once_with("AAPL")
                self.assertEqual(result.decisions[0].fill.quantity, 3)
                self.assertEqual(broker.submit.call_args.args[0].reason, "trailing stop")
                self.assertEqual(broker.positions.get("AAPL", Position("AAPL")).quantity, 0)

    def test_stop_fill_during_cancellation_only_exits_remaining_shares(self):
        for remaining in (0, 1):
            with self.subTest(remaining=remaining), TemporaryDirectory() as directory:
                broker = PaperBroker(10000)
                broker.positions = {"AAPL": Position("AAPL", 3, 100)}
                broker.position_prices = {"AAPL": 104}
                broker.open_orders = Mock(return_value=[remote_order(side="sell", status="new", stop_price="95", qty="3")])
                def confirm_cancel(symbol):
                    broker.positions = {symbol: Position(symbol, remaining, 100)} if remaining else {}
                    broker.open_orders.return_value = []
                broker.cancel_open_sell_orders = Mock(side_effect=confirm_cancel)
                broker.submit = Mock(wraps=broker.submit)
                engine = self.make_engine(broker, directory)
                engine.journal.update_position_highs({"AAPL": 110}, {"AAPL"})
                engine.run_risk_once(submit_orders=True)
                if remaining:
                    self.assertEqual(broker.submit.call_args.args[0].quantity, remaining)
                else:
                    broker.submit.assert_not_called()
                self.assertEqual(broker.positions.get("AAPL", Position("AAPL")).quantity, 0)

    def test_missing_current_mark_cannot_fall_back_to_stale_daily_close(self):
        broker = PaperBroker(10000)
        broker.positions = {"AAPL": Position("AAPL", 3, 100)}
        broker.position_prices = {}
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "current broker marks"):
                self.make_engine(broker, directory).run_risk_once()


if __name__ == "__main__":
    unittest.main()
