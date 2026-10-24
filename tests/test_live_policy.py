from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_bot.alpaca_integration import _load_alpaca_modules
from trading_bot.fx_budget import FxQuote
from trading_bot.live_trading import AlpacaLiveBroker, LivePolicy, main
from trading_bot.models import Order, Position, Side
from tests.test_live_risk import ACCOUNT, account, configured_broker
from tests.test_buy_revalidation import CurrentBroker, make_engine


class LivePolicyTests(unittest.TestCase):
    def test_user_limits_and_nonfinite_budgets_cannot_be_enabled(self):
        for values in (
            dict(capital_limit_eur=5001), dict(max_total_loss_eur=1001),
            dict(max_daily_loss_eur=201), dict(max_order_eur=101),
            dict(capital_limit_eur=True), dict(max_total_loss_eur=float('nan')),
            dict(max_daily_loss_eur=0), dict(entry_limit_bps=26), dict(enabled='true'),
        ):
            with self.subTest(values=values):
                parameters = dict(account_id=ACCOUNT, capital_limit_eur=1000, max_total_loss_eur=200,
                                  max_daily_loss_eur=100, max_order_eur=100)
                parameters.update(values)
                with self.assertRaises(ValueError):
                    LivePolicy(**parameters)

    def test_duplicate_policy_fields_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text('{"enabled":false,"enabled":true}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                LivePolicy.load(path)

    def test_enabling_does_not_change_policy_fingerprint_or_reset_risk(self):
        disabled = LivePolicy(ACCOUNT, 1000, 200, 100, 100)
        enabled = LivePolicy(ACCOUNT, 1000, 200, 100, 100, enabled=True)
        self.assertEqual(disabled.fingerprint, enabled.fingerprint)
        self.assertNotEqual(enabled.fingerprint, LivePolicy(ACCOUNT, 900, 200, 100, 90, enabled=True).fingerprint)

    def test_missing_policy_and_missing_exact_confirmation_never_create_a_broker(self):
        with TemporaryDirectory() as directory, patch('trading_bot.live_trading.AlpacaLiveBroker') as broker:
            path = Path(directory) / 'policy.json'
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['check', '--policy', str(path)]), 2)
            policy = LivePolicy(ACCOUNT, 1000, 200, 100, 100, enabled=True)
            from dataclasses import asdict
            path.write_text(json.dumps(asdict(policy)), encoding='utf-8')
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['run', '--policy', str(path)]), 2)
            broker.assert_not_called()

    def test_readonly_constructor_uses_live_keys_fixed_endpoint_and_zero_sdk_retries(self):
        with TemporaryDirectory() as directory:
            modules = _load_alpaca_modules()
            client = Mock()
            client.get_account.return_value = account()
            client.get_all_positions.return_value = []
            modules['TradingClient'] = Mock(return_value=client)
            with patch('trading_bot.live_trading._load_alpaca_modules', return_value=modules), \
                 patch('trading_bot.live_trading._live_keys', return_value=('local-test-live', 'local-test-secret')), \
                 patch('trading_bot.live_trading._configure_request_timeouts', side_effect=lambda value: value):
                broker = AlpacaLiveBroker(LivePolicy(ACCOUNT, 1000, 200, 100, 100), directory=directory,
                                          fx=FxQuote('1.1', date.today()))
            modules['TradingClient'].assert_called_once_with('local-test-live', 'local-test-secret', paper=False)
            self.assertEqual(client._retry, 0)
            self.assertFalse(broker.submit_enabled)
            self.assertEqual(broker.entry_limit_bps, 5)
            self.assertFalse((Path(directory) / 'live-risk.json').exists())
            client.submit_order.assert_not_called()
            client.replace_order_by_id.assert_not_called()
            client.cancel_order_by_id.assert_not_called()

    def test_account_restrictions_and_exact_raw_fractions_block_before_writes(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            for field, bad in [('id', 'different'), ('currency', 'EUR'), ('status', 'CLOSED'),
                               ('trading_blocked', True), ('account_blocked', None),
                               ('trade_suspended_by_user', True), ('equity', 'nan')]:
                candidate = account()
                setattr(candidate, field, bad)
                with self.subTest(field=field), self.assertRaises(RuntimeError):
                    broker._validate_account(candidate)
            broker.positions['A'] = Position('A', 10, 100, broker_quantity='10.000000000000000001')
            with self.assertRaisesRegex(RuntimeError, 'fractional'):
                broker._validate_account(account())

    def test_capital_and_absolute_order_limits_constrain_large_account_cash(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.account = account(100000)
            broker.cash = 100000
            self.assertEqual(broker.sizing_equity(100000), 1000)
            self.assertEqual(broker.constrain_buy_quantity('A', 100, 40), 2)
            broker.positions['B'] = Position('B', 9, 100)
            broker.position_prices['B'] = 100
            self.assertEqual(broker.constrain_buy_quantity('A', 100, 60), 1)

    def test_buy_uses_cap_and_protection_completes_before_the_next_buy(self):
        with TemporaryDirectory() as directory:
            broker = CurrentBroker()
            broker.sizing_equity = lambda _equity: 1000
            broker.constrain_buy_quantity = lambda _symbol, quantity, _ask: min(quantity, 1)
            events = []
            broker.submit.side_effect = lambda order, day: events.append('buy ' + order.symbol) or broker._submit(order, day)
            broker.after_buy_submission = lambda: events.append('protect')
            result = make_engine(directory, broker, [{'A': 100}, {'B': 100}, {'A': 100}, {'B': 100}],
                                 symbols=['A', 'B']).run_once(submit_orders=True)
            self.assertEqual(events, ['buy A', 'protect', 'buy B', 'protect'])
            self.assertEqual([decision.order.quantity for decision in result.decisions], [1, 1])

    def test_failed_entry_protection_latches_halt_before_any_further_buy(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.positions['A'] = Position('A', 1, 100)
            with patch('trading_bot.cli._alpaca_protect', side_effect=RuntimeError('stop not confirmed')), \
                 patch.object(broker, '_emergency_risk_exit') as emergency:
                with self.assertRaisesRegex(RuntimeError, 'stop not confirmed'):
                    broker.after_buy_submission()
            emergency.assert_called_once()
            self.assertTrue(broker.journal._read_state()['drawdown_halted'])

    def test_risk_activity_outage_blocks_buys_but_keeps_known_position_protection_available(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker._fx_injected = True
            broker.positions['A'] = Position('A', 1, 100)
            broker.position_prices['A'] = 100
            broker._observe_risk = Mock(side_effect=RuntimeError('activities unavailable'))
            with patch('trading_bot.alpaca_integration.AlpacaPaperBroker.refresh'), redirect_stdout(StringIO()):
                broker.refresh()
                self.assertIn('activities unavailable', broker.buy_rejection())
                broker._validate_trading_request('protect', order=Order('A', Side.SELL, 1, 90, 'protect'))
            self.assertEqual(broker.constrain_buy_quantity('B', 1, 50), 0)

    def test_funding_and_buying_power_constraints_do_not_change_reported_account_equity(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.cash = 50
            broker.account = account(1000)
            self.assertEqual(broker.snapshot({}).equity, 1000)
            self.assertEqual(broker.snapshot({}).cash, 1000)

    def test_stop_during_live_preflight_is_preserved_and_prevents_operator_start(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            runtime = directory / 'runtime'
            policy = LivePolicy(ACCOUNT, 1000, 200, 100, 100, enabled=True)
            from dataclasses import asdict
            path = directory / 'policy.json'
            path.write_text(json.dumps(asdict(policy)), encoding='utf-8')
            def preflight(_policy):
                (runtime / 'STOP').touch()
                return {'reconciliation_ok': True, 'technical_checks_passed': True}
            with patch('trading_bot.live_trading.live_directory', return_value=runtime), \
                 patch('trading_bot.live_trading.runtime_home', return_value=directory / 'home'), \
                 patch('trading_bot.live_trading.check', side_effect=preflight), \
                 patch('trading_bot.live_trading.AlpacaLiveBroker') as broker, \
                 patch('trading_bot.cli._run_alpaca_session') as session, redirect_stdout(StringIO()):
                self.assertEqual(main(['run', '--policy', str(path), '--confirm-account', ACCOUNT]), 2)
            self.assertTrue((runtime / 'STOP').exists())
            broker.assert_not_called()
            session.assert_not_called()

    def test_pending_stop_confirms_same_entry_before_any_loss_halt(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.positions['A'] = Position('A', 1, 100)
            broker.open_orders.return_value = [SimpleNamespace(symbol='A', side='sell', type='stop', status='new',
                                                             qty='1', filled_qty='0', stop_price='90',
                                                             client_order_id='bot-protect-a')]
            with patch('trading_bot.cli._alpaca_protect', side_effect=[RuntimeError('status pending_new'), None]) as protect, \
                 patch('trading_bot.live_trading.time.sleep') as sleep:
                broker.after_buy_submission()
            self.assertEqual(protect.call_count, 2)
            sleep.assert_called_once_with(1.0)
            self.assertFalse(broker.journal._read_state().get('drawdown_halted', False))

    def test_sdk_specific_activity_error_does_not_disable_existing_position_exits(self):
        class SimulatedSdkError(Exception):
            pass
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker._fx_injected = True
            broker.positions['A'] = Position('A', 1, 100)
            broker.position_prices['A'] = 100
            broker._observe_risk = Mock(side_effect=SimulatedSdkError('activity SDK unavailable'))
            with patch('trading_bot.alpaca_integration.AlpacaPaperBroker.refresh'), redirect_stdout(StringIO()):
                broker._validate_trading_request('submit', order=Order('A', Side.SELL, 1, 100, 'exit'))
            self.assertIn('SDK unavailable', broker.risk_error)
            self.assertEqual(broker.constrain_buy_quantity('B', 1, 50), 0)

    def test_existing_same_day_funding_is_not_counted_twice_on_initialization(self):
        from tests.test_live_risk import observe
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker._activities.return_value = [{'id': 'funding-before-run', 'activity_type': 'CSD',
                                                'net_amount': '1000', 'status': 'executed'}]
            state = observe(broker)
            self.assertEqual(state['normalized_equity_eur'], 1000)
            self.assertEqual(state['cash_flows_usd'], 0)
            self.assertFalse(state.get('loss_halted', False))

    def test_generated_replacement_stop_id_remains_confirmed_owned_protection(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.positions['A'] = Position('A', 1, 100)
            broker.journal.mark_protective_order('A', 'broker-generated-replacement', 90, 1)
            broker.open_orders.return_value = [SimpleNamespace(symbol='A', side='sell', type='stop', status='new',
                                                             qty='1', filled_qty='0', stop_price='90',
                                                             client_order_id='broker-generated-replacement')]
            with patch('trading_bot.cli._alpaca_protect'):
                broker.after_buy_submission()
            self.assertFalse(broker.journal._read_state().get('drawdown_halted', False))

    def test_tracked_generated_sell_stop_can_be_canceled_before_a_risk_exit(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.refresh = Mock()
            broker.journal.mark_protective_order('A', 'broker-generated-replacement', 90, 1)
            remote = SimpleNamespace(id='remote-id', side='sell', client_order_id='broker-generated-replacement')
            broker.open_orders.side_effect = [[remote], [remote], []]
            self.assertEqual(broker.cancel_open_sell_orders('A'), 1)
            broker._client.cancel_order_by_id.assert_called_once_with('remote-id')

    def test_foreign_sell_is_not_canceled_by_live_management(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            broker.open_orders.return_value = [SimpleNamespace(id='manual-id', side='sell', client_order_id='manual')]
            with self.assertRaisesRegex(RuntimeError, 'External SELL'):
                broker.cancel_open_sell_orders('A')
            broker._client.cancel_order_by_id.assert_not_called()

    def test_unavailable_emergency_exit_preserves_halt_and_original_protection_error(self):
        with TemporaryDirectory() as directory:
            broker = configured_broker(directory)
            with patch('trading_bot.cli._alpaca_protect', side_effect=RuntimeError('stop rejected')), \
                 patch.object(broker, '_emergency_risk_exit', side_effect=RuntimeError('broker offline')):
                with self.assertRaisesRegex(RuntimeError, 'stop rejected.*broker offline'):
                    broker.after_buy_submission()
            self.assertTrue(broker.journal._read_state()['drawdown_halted'])


if __name__ == '__main__':
    unittest.main()
