"""Existing and replaced stops need the same authoritative confirmation as new stops."""
import argparse
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_bot.cli import _alpaca_protect
from trading_bot.models import Position


CLIENT_ID = 'bot-protect-aapl-2026-10-22'


def stop(**overrides):
    values = dict(id='stop-1', client_order_id=CLIENT_ID, symbol='AAPL', side='sell', type='stop',
                  status='new', qty='5', filled_qty='0', stop_price='90')
    values.update(overrides)
    return SimpleNamespace(**values)


class LiveProtectionConfirmationTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.broker = stack.enter_context(patch('trading_bot.cli._session_broker')).return_value
        self.journal = stack.enter_context(patch('trading_bot.cli.TradeJournal')).return_value
        stack.enter_context(patch('trading_bot.cli._sync_alpaca_protective_fills'))
        stack.enter_context(patch('trading_bot.cli._sync_position_highs_from_positions'))
        stack.enter_context(patch('trading_bot.cli.reconcile_positions', return_value=SimpleNamespace(ok=True)))
        stack.enter_context(patch('trading_bot.cli._position_risk_rows', return_value=[{'symbol': 'AAPL', 'active_stop': 90.0}]))
        self.broker.positions = {'AAPL': Position('AAPL', 5, 100)}
        self.journal._read_state.return_value = {}
        self.args = argparse.Namespace(journal_dir='unused', submit=True, stop_loss_pct=.1, trailing_stop_pct=.1)

    def test_unchanged_stop_requires_confirmed_active_status(self):
        for status in ('pending_new', 'suspended', 'unknown', 'done_for_day'):
            with self.subTest(status=status):
                self.journal.reset_mock()
                self.broker.open_orders.return_value = [stop(status=status)]
                with self.assertRaisesRegex(RuntimeError, 'not confirmed active'):
                    _alpaca_protect(self.args)
                self.journal.mark_protective_order.assert_not_called()
        self.broker.submit_protective_stop.assert_not_called()

    def test_active_existing_stop_is_recorded_with_its_confirmed_price(self):
        self.broker.open_orders.return_value = [stop(status='accepted', stop_price='92')]
        _alpaca_protect(self.args)
        self.journal.mark_protective_order.assert_called_once_with('AAPL', CLIENT_ID, 92.0, 5)
        self.broker.replace_stop_order.assert_not_called()

    def test_pending_replacement_is_not_recorded_as_confirmed_protection(self):
        self.broker.open_orders.return_value = [stop(stop_price='89')]
        self.broker.replace_stop_order.return_value = stop(status='pending_replace', stop_price='90')
        with self.assertRaisesRegex(RuntimeError, 'not confirmed active'):
            _alpaca_protect(self.args)
        self.journal.mark_protective_order.assert_not_called()

    def test_replacement_must_confirm_exact_quantity_identity_and_minimum_stop(self):
        self.broker.open_orders.return_value = [stop(stop_price='89')]
        for overrides in ({'qty': '4'}, {'symbol': 'MSFT'}, {'side': 'buy'}, {'type': 'limit'},
                          {'stop_price': '89'}, {'filled_qty': '1'}, {'stop_price': 'nan'}):
            with self.subTest(overrides=overrides):
                self.journal.reset_mock()
                self.broker.replace_stop_order.return_value = stop(**overrides)
                with self.assertRaises(RuntimeError):
                    _alpaca_protect(self.args)
                self.journal.mark_protective_order.assert_not_called()

    def test_active_replacement_can_confirm_broker_generated_client_id(self):
        self.broker.open_orders.return_value = [stop(qty='10', stop_price='95')]
        self.broker.replace_stop_order.return_value = stop(client_order_id='broker-replacement-id', stop_price='95')
        _alpaca_protect(self.args)
        self.broker.replace_stop_order.assert_called_once_with('stop-1', 95.0, 5)
        self.journal.mark_protective_order.assert_called_once_with('AAPL', 'broker-replacement-id', 95.0, 5)

    def test_partially_filled_pending_and_foreign_stops_remain_untouched(self):
        for overrides in ({'filled_qty': '1', 'status': 'partially_filled'}, {'status': 'pending_cancel'},
                          {'status': 'pending_replace'}, {'client_order_id': 'manually-managed'}):
            with self.subTest(overrides=overrides):
                self.broker.reset_mock()
                self.journal.reset_mock()
                self.broker.open_orders.return_value = [stop(**overrides)]
                _alpaca_protect(self.args)
                self.broker.replace_stop_order.assert_not_called()
                self.broker.submit_protective_stop.assert_not_called()
                self.journal.mark_protective_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
