from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_bot.journal import TradeJournal
from trading_bot.live_trading import AlpacaLiveBroker, LivePolicy
from trading_bot.models import Fill, Order, Position, Side
from trading_bot.reconcile import sync_broker_orders


ACCOUNT = '11111111-1111-4111-8111-111111111111'
STAMP = datetime(2026, 10, 22, 17, tzinfo=timezone.utc)


class ReferenceFx:
    usd_per_eur = 1.0
    as_of = date(2026, 10, 22)
    date = as_of

    def usd_to_eur(self, value):
        return float(value)

    def eur_to_usd(self, value, **_kwargs):
        return float(value)

    def validate(self, **_kwargs):
        return self


def account(equity=1000):
    return SimpleNamespace(id=ACCOUNT, status='ACTIVE', currency='USD', equity=str(equity),
                           cash=str(equity), buying_power=str(equity), non_marginable_buying_power=str(equity),
                           trading_blocked=False, account_blocked=False, trade_suspended_by_user=False)


def configured_broker(directory, *, submit_enabled=True):
    value = AlpacaLiveBroker.__new__(AlpacaLiveBroker)
    value.policy = LivePolicy(ACCOUNT, 1000, 200, 100, 100, enabled=True)
    value.submit_enabled = submit_enabled
    value.directory = Path(directory)
    value.journal = TradeJournal(directory)
    value.fx = ReferenceFx()
    value.account = account()
    value.account_id = ACCOUNT
    value.positions, value.position_prices, value.fills = {}, {}, []
    value.cash = 1000
    value.last_submitted_order = None
    value._client = Mock()
    value._client.get_account.side_effect = lambda: value.account
    value._activities = Mock(return_value=[])
    value.open_orders = Mock(return_value=[])
    value.closed_orders = Mock(return_value=[])
    value._order_side = SimpleNamespace(BUY='buy', SELL='sell')
    value._order_type = SimpleNamespace(STOP='stop')
    value._time_in_force = SimpleNamespace(DAY='day', IOC='ioc', GTC='gtc')
    for name in ('_limit_order_request', '_market_order_request', '_stop_order_request', '_replace_order_request'):
        setattr(value, name, lambda **kwargs: SimpleNamespace(**kwargs))
    value._configure_entry_policy(5)
    return value


def observe(broker, stamp=STAMP):
    with patch('trading_bot.live_trading.datetime', wraps=datetime) as clock:
        clock.now.return_value = stamp
        broker._observe_risk()
    return json.loads((broker.directory / 'live-risk.json').read_text(encoding='utf-8'))


class LiveRiskTests(unittest.TestCase):
    def test_readonly_live_adapter_blocks_every_inherited_broker_mutation(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory, submit_enabled=False)
            value.refresh = Mock()
            value.order_by_client_id = Mock(return_value=None)
            value.positions['AAPL'] = Position('AAPL', 2, 100)
            buy = Order('AAPL', Side.BUY, 1, 100, 'entry', 'bot-test-buy')
            sell = Order('AAPL', Side.SELL, 1, 100, 'exit', 'bot-test-sell')
            actions = [lambda: value.submit(buy), lambda: value.submit(sell),
                       lambda: value.submit_protective_stop('AAPL', 2, 90, 'bot-protect-test'),
                       lambda: value.replace_stop_order('stop-id', 95, 2)]
            for action in actions:
                with self.subTest(action=action):
                    with self.assertRaisesRegex(RuntimeError, 'Read-only'):
                        action()
            sell_remote = SimpleNamespace(id='sell-id', client_order_id='bot-test-sell', symbol='AAPL',
                                          side='sell', status='new')
            value.open_orders.return_value = [sell_remote]
            with self.assertRaisesRegex(RuntimeError, 'Read-only'):
                value.cancel_open_sell_orders('AAPL')
            buy_remote = SimpleNamespace(id='buy-id', client_order_id='bot-test-buy', symbol='AAPL',
                                         side='buy', status='new')
            value.open_orders.return_value = [buy_remote]
            with self.assertRaisesRegex(RuntimeError, 'Read-only'):
                value.cancel_open_buy_orders({'bot-test-buy'})
            value._client.submit_order.assert_not_called()
            value._client.replace_order_by_id.assert_not_called()
            value._client.cancel_order_by_id.assert_not_called()

    def test_recovered_live_execution_uses_live_fill_mode(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory, submit_enabled=False)
            value.refresh = Mock()
            order = SimpleNamespace(id='broker-1', client_order_id='bot-live-buy', symbol='AAPL', side='buy',
                                    qty='1', filled_qty='1', filled_avg_price='100', status='filled',
                                    filled_at=STAMP)
            value.closed_orders.return_value = [order]
            self.assertEqual(sync_broker_orders(value.journal, value), 1)
            text = value.journal.fills_path.read_text(encoding='utf-8')
            self.assertIn('alpaca-live-sync', text)
            self.assertNotIn('alpaca-paper-sync', text)

    def test_first_flat_initialization_records_risk_locally_without_broker_writes(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            state = observe(value)
            self.assertEqual(state['normalized_equity_eur'], 1000)
            self.assertEqual(state['total_loss_eur'], 0)
            self.assertEqual(state['daily_loss_eur'], 0)
            value._client.submit_order.assert_not_called()
            value._client.replace_order_by_id.assert_not_called()
            value._client.cancel_order_by_id.assert_not_called()

    def test_external_deposit_and_withdrawal_do_not_create_profit_or_drawdown(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            value.account = account(1200)
            value._activities.return_value = [{'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '200',
                                               'status': 'executed'}]
            deposited = observe(value)
            self.assertEqual(deposited['normalized_equity_eur'], 1000)
            self.assertEqual(deposited['total_loss_eur'], 0)
            value.account = account(900)
            value._activities.return_value = [{'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '200',
                                               'status': 'executed'},
                                              {'id': 'withdrawal', 'activity_type': 'CSW', 'net_amount': '-300',
                                               'status': 'executed'}]
            withdrawn = observe(value)
            self.assertEqual(withdrawn['normalized_equity_eur'], 1000)
            self.assertEqual(withdrawn['cash_flows_usd'], -100)
            self.assertFalse(value.journal._read_state().get('drawdown_halted', False))

    def test_loss_limit_equality_latches_across_recovery_and_restart(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            value.account = account(900)
            triggered = observe(value)
            self.assertEqual(triggered['daily_loss_eur'], 100)
            self.assertTrue(triggered['loss_halted'])
            trigger = value.journal._read_state()['drawdown_halt']
            restarted = configured_broker(directory)
            restarted.account = account(1100)
            recovered = observe(restarted)
            self.assertTrue(recovered['loss_halted'])
            self.assertEqual(restarted.journal._read_state()['drawdown_halt'], trigger)

    def test_policy_account_change_does_not_reset_saved_loss_baseline(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            path = value.directory / 'live-risk.json'
            original = path.read_bytes()
            value.policy = LivePolicy(ACCOUNT, 900, 180, 90, 90, enabled=True)
            with self.assertRaisesRegex(RuntimeError, 'policy/account changed'):
                observe(value)
            self.assertEqual(path.read_bytes(), original)

    def test_unsupported_corporate_action_requires_review_without_adjusting_share_ledger(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            value._activities.return_value = [{'id': 'split', 'activity_type': 'SPLIT', 'status': 'executed'}]
            state = observe(value)
            self.assertIn('SPLIT', state['review_required'])
            self.assertEqual(value.journal.expected_positions_from_fills(), {})

    def test_overnight_loss_counts_toward_new_market_days_loss_limit(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value, datetime(2026, 10, 21, 19, tzinfo=timezone.utc))
            value.account = account(850)
            next_day = observe(value)
            self.assertGreaterEqual(next_day['daily_loss_eur'], 150)
            self.assertTrue(next_day['loss_halted'])

    def test_missing_risk_state_does_not_rebaseline_after_a_completed_roundtrip(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            for side in (Side.BUY, Side.SELL):
                value.journal.record_fill(Fill(date(2026, 10, 21), 'AAPL', side, 1, 100, 100, 'past'), 'alpaca-live')
                value.journal.mark_submitted(f'2026-10-21:AAPL:{side.value}')
            self.assertEqual(value.journal.expected_positions_from_fills(), {})
            with self.assertRaisesRegex(RuntimeError, 'initial|baseline|journal|history'):
                observe(value)
            self.assertFalse((value.directory / 'live-risk.json').exists())

    def test_deleted_risk_file_does_not_clear_initialization_marker_on_restart(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            (value.directory / 'live-risk.json').unlink()
            restarted = configured_broker(directory)
            with self.assertRaisesRegex(RuntimeError, 'initial|baseline|journal|history'):
                observe(restarted)
            self.assertFalse((value.directory / 'live-risk.json').exists())

    def test_cash_dividend_is_income_and_does_not_get_subtracted_as_a_deposit(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            value.account = account(1010)
            value._activities.return_value = [{'id': 'dividend', 'activity_type': 'DIV', 'net_amount': '10',
                                               'status': 'executed'}]
            state = observe(value)
            self.assertEqual(state['cash_flows_usd'], 0)
            self.assertEqual(state['normalized_equity_eur'], 1010)

    def test_corrected_or_canceled_cash_activity_requires_review_without_assuming_cash_effect(self):
        for status in ('correct', 'canceled'):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                value = configured_broker(directory)
                observe(value)
                value._activities.return_value = [{'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '500',
                                                   'status': status}]
                state = observe(value)
                self.assertIn('review_required', state)
                self.assertEqual(state['cash_flows_usd'], 0)

    def test_same_activity_id_with_changed_amount_cannot_silently_change_external_flow(self):
        with TemporaryDirectory() as directory:
            value = configured_broker(directory)
            observe(value)
            value.account = account(1100)
            value._activities.return_value = [{'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '100',
                                               'status': 'executed'}]
            observe(value)
            value._activities.return_value = [{'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '500',
                                               'status': 'executed'}]
            state = observe(value)
            self.assertIn('corrected', state['review_required'])
            self.assertEqual(state['cash_flows_usd'], 100)


if __name__ == '__main__':
    unittest.main()
