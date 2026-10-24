from __future__ import annotations

import csv
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from trading_bot.journal import TradeJournal
from trading_bot.models import Fill, Order, Position, Side
from trading_bot.reconcile import reconcile_positions, sync_broker_orders


DAY = date(2026, 10, 22)


def remote(**changes):
    values = dict(id="broker-1", client_order_id="bot-accounting-1", symbol="AAPL", side="buy",
                  qty="10", filled_qty="10", filled_avg_price="100", status="filled",
                  filled_at=datetime(2026, 10, 22, 15, tzinfo=timezone.utc))
    values.update(changes)
    return SimpleNamespace(**values)


def broker(**positions):
    return SimpleNamespace(positions=positions)


class FundedAccountingTests(unittest.TestCase):
    def test_matching_whole_positions_reconcile_with_structured_empty_issues(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote())
            result = reconcile_positions(journal, broker(AAPL=Position("AAPL", 10, 100, "10.000000000")))
            self.assertTrue(result.ok)
            self.assertEqual(result.issues, [])

    def test_matching_fractional_partial_fill_is_truthful_but_blocks_unsupported_exits(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote(status="partially_filled", filled_qty="0.125"))
            result = reconcile_positions(journal, broker(AAPL=Position("AAPL", 0.125, 100, "0.125")))
            self.assertFalse(result.ok)
            self.assertEqual([issue.code for issue in result.issues], ["unsupported_fractional_position"])
            self.assertEqual(journal.expected_positions_from_fills(), {"AAPL": 0.125})
            self.assertIn("protective stop coverage", result.issues[0].action)

    def test_broker_decimal_representation_reveals_remainder_float_would_hide(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote())
            result = reconcile_positions(journal, broker(AAPL=Position("AAPL", 10, 100, "10.000000000000000001")))
            self.assertFalse(result.ok)
            mismatch = next(issue for issue in result.issues if issue.code == "position_quantity_mismatch")
            self.assertEqual(mismatch.actual_quantity, "10.000000000000000001")

    def test_possible_split_is_not_inferred_applied_or_journal_reset(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote())
            original = journal.fills_path.read_bytes(), journal.state_path.read_bytes()
            result = reconcile_positions(journal, broker(AAPL=Position("AAPL", 20, 50, "20")))
            self.assertEqual([issue.code for issue in result.issues], ["position_quantity_mismatch"])
            self.assertIn("splits", result.issues[0].action)
            self.assertEqual((journal.fills_path.read_bytes(), journal.state_path.read_bytes()), original)

    def test_symbol_change_is_external_change_requiring_broker_review(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote())
            result = reconcile_positions(journal, broker(NEW=Position("NEW", 10, 100)))
            self.assertFalse(result.ok)
            self.assertEqual({issue.code for issue in result.issues},
                             {"position_quantity_mismatch", "untracked_broker_position"})

    def test_matching_short_positions_still_block_long_only_operator(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote(side="sell"))
            result = reconcile_positions(journal, broker(AAPL=Position("AAPL", -10, 100)))
            self.assertEqual([issue.code for issue in result.issues], ["unsupported_short_position"])
            self.assertFalse(result.ok)

    def test_invalid_broker_quantity_is_classified(self):
        for quantity in ("NaN", "Infinity", "broken"):
            with self.subTest(quantity=quantity), TemporaryDirectory() as directory:
                result = reconcile_positions(TradeJournal(directory), broker(AAPL=Position("AAPL", 0, 100, quantity)))
                self.assertFalse(result.ok)
                self.assertEqual(result.issues[0].code, "invalid_broker_quantity")

    def test_exact_fractional_cumulative_deltas_survive_roundtrip(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            order = remote(status="partially_filled", filled_qty="0.123456789123456789")
            journal.record_broker_order(order)
            order.filled_qty = "0.223456789123456789"
            journal.record_broker_order(order)
            self.assertEqual(journal.expected_positions_decimal_from_fills(),
                             {"AAPL": Decimal("0.223456789123456789")})
            self.assertIsNone(TradeJournal(directory).record_broker_order(order))
            order.id, order.client_order_id, order.side = "sell-1", "bot-accounting-sell", "sell"
            journal.record_broker_order(order)
            self.assertEqual(journal.expected_positions_decimal_from_fills(), {})

    def test_same_broker_id_cannot_silently_change_execution_identity(self):
        for changes in ({"symbol": "MSFT"}, {"side": "sell"}, {"client_order_id": "another-id"}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                journal.record_broker_order(remote())
                original = journal.fills_path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    journal.record_broker_order(remote(**changes))
                self.assertEqual(journal.fills_path.read_bytes(), original)

    def test_same_client_id_cannot_appear_with_new_broker_order(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_broker_order(remote())
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                journal.record_broker_order(remote(id="another-broker-id"))
            self.assertEqual(journal.expected_positions_from_fills(), {"AAPL": 10})

    def test_overfilled_or_corrected_execution_requires_review_without_mutation(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                journal.record_broker_order(remote(filled_qty="11"))
            self.assertFalse(journal.fills_path.exists())
            journal.record_broker_order(remote())
            original = journal.fills_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "corrected"):
                journal.record_broker_order(remote(filled_avg_price="100.00001"))
            self.assertEqual(journal.fills_path.read_bytes(), original)

    def test_corrupt_cursor_cannot_hide_in_expected_positions(self):
        for field, value in (("broker_filled_quantity", "11"), ("broker_filled_value", "NaN"),
                             ("broker_filled_value", "999"), ("client_order_id", "")):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                journal = TradeJournal(directory)
                journal.record_broker_order(remote())
                with journal.fills_path.open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    headers, rows = reader.fieldnames, list(reader)
                rows[0][field] = value
                journal._replace_csv(journal.fills_path, headers, rows)
                original = journal.fills_path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                    journal.expected_positions_decimal_from_fills()
                with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                    journal.record_broker_order(remote())
                self.assertEqual(journal.fills_path.read_bytes(), original)

    def test_invalid_confirmed_legacy_identity_cannot_be_ignored_as_flat(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.record_fill(Fill(DAY, "AAPL", Side.HOLD, 1, 100, 100, "invalid"), "alpaca-paper")
            journal.mark_submitted(f"{DAY}:AAPL:hold")
            with self.assertRaisesRegex(RuntimeError, "invalid execution identity"):
                journal.expected_positions_from_fills()

    def test_completed_modern_protective_execution_is_rechecked_for_broker_corrections(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            order = remote(client_order_id="bot-protect-aapl-test", side="sell")
            journal.record_broker_order(order)
            journal.mark_synced_order(order.client_order_id)
            view = SimpleNamespace(closed_orders=lambda: [order], positions={})
            self.assertEqual(sync_broker_orders(journal, view), 0)
            original = journal.fills_path.read_bytes()
            order.filled_avg_price = "100.01"
            with self.assertRaisesRegex(RuntimeError, "corrected"):
                sync_broker_orders(journal, view)
            self.assertEqual(journal.fills_path.read_bytes(), original)

    def test_legacy_synced_protective_execution_without_cursor_is_not_double_imported(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            order = remote(client_order_id="bot-protect-aapl-legacy", side="sell")
            journal.record_fill(Fill(DAY, "AAPL", Side.SELL, 10, 100, 1000, "legacy"), "alpaca-paper")
            journal.mark_submitted(f"{DAY}:AAPL:sell")
            journal.mark_synced_order(order.client_order_id)
            original = journal.fills_path.read_bytes()
            self.assertEqual(sync_broker_orders(journal, SimpleNamespace(closed_orders=lambda: [order], positions={})), 0)
            self.assertEqual(journal.fills_path.read_bytes(), original)

    def test_absolute_loss_halt_persists_and_blocks_buys_without_percentage_breach(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_drawdown_state(1000, 0.15)
            payload = {"loss": 20.0, "limit": 15.0, "market_date": str(DAY)}
            self.assertTrue(journal.latch_risk_halt("daily_loss_limit", payload))
            payload["loss"] = 0
            restarted = TradeJournal(directory)
            halt = restarted._read_state()["drawdown_halt"]
            self.assertEqual(halt["reason"], "daily_loss_limit")
            self.assertEqual(halt["metadata"]["loss"], 20.0)
            self.assertIn("triggered_at", halt)
            self.assertTrue(restarted.update_drawdown_state(1100, 0.5)[2])
            self.assertFalse(restarted.reserve_order(Order("AAPL", Side.BUY, 1, 100, "entry"), DAY))
            self.assertTrue(restarted.reserve_order(Order("AAPL", Side.SELL, 1, 100, "exit"), DAY))

    def test_absolute_loss_reason_preserves_existing_halt_and_rejects_invalid_metadata(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_drawdown_state(1000, 0.15)
            journal.update_drawdown_state(800, 0.15)
            original_halt = journal._read_state()["drawdown_halt"]
            journal.latch_risk_halt("total_loss_limit", {"loss": 200, "limit": 100})
            journal.latch_risk_halt("total_loss_limit", {"loss": 200, "limit": 100})
            state = journal._read_state()
            self.assertEqual(state["drawdown_halt"], original_halt)
            self.assertEqual(len(state["risk_halt_events"]), 1)
            self.assertEqual(state["risk_halt_events"][0]["reason"], "total_loss_limit")
            original = journal.state_path.read_bytes()
            for reason, metadata in ((" ", {}), ("reason", []), ("reason", {"loss": float("nan")}),
                                     ("reason", {"loss": float("inf")}), ("reason", {"loss": object()})):
                with self.subTest(reason=reason, metadata=metadata):
                    with self.assertRaises(ValueError):
                        journal.latch_risk_halt(reason, metadata)
                    self.assertEqual(journal.state_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
