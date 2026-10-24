import argparse
import contextlib
from datetime import date
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_bot.cli import _alpaca_protect, _protective_stop_attempt
from trading_bot.models import Position


TRADE_DATE = date(2026, 9, 22)
BASE_ID = "bot-protect-aapl-2026-09-22"


def remote_stop(client_order_id=BASE_ID, status="new", **overrides):
    values = dict(
        id="broker-stop", client_order_id=client_order_id, symbol="AAPL", side="sell",
        type="stop", status=status, filled_qty="0", qty="5", stop_price="90",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ProtectiveRetryTests(unittest.TestCase):
    def setUp(self):
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.broker = self.context.enter_context(patch("trading_bot.cli.AlpacaPaperBroker")).return_value
        self.journal = self.context.enter_context(patch("trading_bot.cli.TradeJournal")).return_value
        self.context.enter_context(patch("trading_bot.cli._sync_alpaca_protective_fills"))
        self.context.enter_context(patch("trading_bot.cli._sync_position_highs_from_positions"))
        self.context.enter_context(patch("trading_bot.cli.reconcile_positions", return_value=SimpleNamespace(ok=True)))
        self.context.enter_context(patch("trading_bot.cli._position_risk_rows", return_value=[
            {"symbol": "AAPL", "active_stop": 90.0},
        ]))
        self.broker.positions = {"AAPL": Position("AAPL", 5, 100)}
        self.broker.open_orders.return_value = []
        self.broker.trading_date.return_value = TRADE_DATE
        self.broker.order_by_client_id.return_value = None
        self.journal._read_state.return_value = {}
        self.args = argparse.Namespace(journal_dir="unused", submit=True, stop_loss_pct=.1, trailing_stop_pct=.1)

    def test_zero_fill_terminal_stop_allows_next_stable_id(self):
        for status in ("canceled", "expired", "rejected"):
            with self.subTest(status=status):
                self.broker.reset_mock()
                self.journal.reset_mock()
                next_id = BASE_ID + "-r2"
                self.broker.order_by_client_id.side_effect = [remote_stop(status=status), None]
                self.broker.submit_protective_stop.return_value = remote_stop(next_id, "accepted")
                _alpaca_protect(self.args)
                self.broker.submit_protective_stop.assert_called_once_with("AAPL", 5, 90.0, next_id)
                self.journal.mark_protective_order.assert_called_once_with("AAPL", next_id, 90.0, 5)

    def test_timeout_retries_same_id_without_advancing(self):
        self.broker.submit_protective_stop.side_effect = [TimeoutError("lost response"), remote_stop()]
        with self.assertRaises(TimeoutError):
            _alpaca_protect(self.args)
        self.journal.mark_protective_order.assert_not_called()
        _alpaca_protect(self.args)
        self.assertEqual(self.broker.submit_protective_stop.call_count, 2)
        self.assertEqual([call.args[-1] for call in self.broker.submit_protective_stop.call_args_list], [BASE_ID, BASE_ID])
        self.journal.mark_protective_order.assert_called_once_with("AAPL", BASE_ID, 90.0, 5)

    def test_active_direct_lookup_preserves_idempotency_when_open_list_lags(self):
        self.broker.order_by_client_id.return_value = remote_stop(stop_price="92")
        _alpaca_protect(self.args)
        self.broker.submit_protective_stop.assert_not_called()
        self.journal.mark_protective_order.assert_called_once_with("AAPL", BASE_ID, 92.0, 5)

    def test_unresolved_statuses_never_advance(self):
        for status in ("", "unknown", "pending_new", "pending_cancel", "pending_replace", "replaced", "filled", "partially_filled", "done_for_day"):
            with self.subTest(status=status):
                self.broker.reset_mock()
                self.broker.order_by_client_id.return_value = remote_stop(status=status)
                with self.assertRaises(RuntimeError):
                    _alpaca_protect(self.args)
                self.broker.order_by_client_id.assert_called_once_with(BASE_ID)
                self.broker.submit_protective_stop.assert_not_called()
                self.journal.mark_protective_order.assert_not_called()

    def test_lookup_failure_never_submits_or_advances(self):
        self.broker.order_by_client_id.side_effect = RuntimeError("broker lookup unavailable")
        with self.assertRaisesRegex(RuntimeError, "lookup unavailable"):
            _alpaca_protect(self.args)
        self.broker.submit_protective_stop.assert_not_called()
        self.journal.mark_protective_order.assert_not_called()

    def test_daily_attempt_limit_includes_recovery_after_three_risk_exits(self):
        expected_ids = [BASE_ID, BASE_ID + "-r2", BASE_ID + "-r3", BASE_ID + "-r4"]
        self.broker.order_by_client_id.side_effect = lambda client_id: remote_stop(client_id, "canceled")
        for _ in range(2):
            self.broker.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "four attempts"):
                _alpaca_protect(self.args)
            self.assertEqual([call.args[0] for call in self.broker.order_by_client_id.call_args_list], expected_ids)
            self.broker.submit_protective_stop.assert_not_called()
        self.journal.mark_protective_order.assert_not_called()

    def test_terminal_stop_identity_must_match_before_retry(self):
        for overrides in ({"symbol": "MSFT"}, {"side": "buy"}, {"type": "market"}, {"client_order_id": "other"}):
            with self.subTest(overrides=overrides):
                broker = Mock()
                broker.order_by_client_id.return_value = remote_stop(status="canceled", **overrides)
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    _protective_stop_attempt(broker, "AAPL", TRADE_DATE)
                broker.order_by_client_id.assert_called_once_with(BASE_ID)

    def test_terminal_stop_requires_explicit_zero_fill(self):
        for filled_qty in (None, "", "nan", "inf", "-1", ".5"):
            with self.subTest(filled_qty=filled_qty):
                broker = Mock()
                broker.order_by_client_id.return_value = remote_stop(status="canceled", filled_qty=filled_qty)
                with self.assertRaises(RuntimeError):
                    _protective_stop_attempt(broker, "AAPL", TRADE_DATE)
                broker.order_by_client_id.assert_called_once_with(BASE_ID)

    def test_rejected_submission_is_not_recorded_as_protection(self):
        self.broker.submit_protective_stop.return_value = remote_stop(status="rejected")
        with self.assertRaisesRegex(RuntimeError, "not confirmed active"):
            _alpaca_protect(self.args)
        self.broker.submit_protective_stop.assert_called_once_with("AAPL", 5, 90.0, BASE_ID)
        self.journal.mark_protective_order.assert_not_called()

    def test_submission_response_must_match_identity_and_required_protection(self):
        for overrides in ({"symbol": "MSFT"}, {"client_order_id": "other"}, {"side": "buy"},
                          {"type": "market"}, {"qty": "4"}, {"stop_price": "89"},
                          {"stop_price": "nan"}, {"status": "pending_new"}, {"filled_qty": "1"}):
            with self.subTest(overrides=overrides):
                self.broker.submit_protective_stop.return_value = remote_stop(**overrides)
                with self.assertRaises(RuntimeError):
                    _alpaca_protect(self.args)
                self.journal.mark_protective_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
