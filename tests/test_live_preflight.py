"""Live preflight checks run against disposable journals and fake market data."""
import contextlib
from datetime import date, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from alpaca.common.exceptions import APIError

from trading_bot.journal import TradeJournal
from trading_bot.live_trading import LivePolicy, check
from trading_bot.models import Candle
from trading_bot.reconcile import ReconciliationResult
from tests.test_live_risk import ACCOUNT, ReferenceFx, account


EXPECTED = date(2026, 10, 21)


def history(symbol, close, *, count=195, final_date=EXPECTED):
    return [Candle(final_date - timedelta(days=count - 1 - index), symbol,
                   close, close, close, close, 1000) for index in range(count)]


class LivePreflightTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        temporary = stack.enter_context(TemporaryDirectory())
        self.directory = Path(temporary)
        self.policy = LivePolicy(ACCOUNT, 1000, 200, 100, 100, enabled=True)
        self.broker = SimpleNamespace(
            directory=self.directory, journal=TradeJournal(self.directory), positions={}, account=account(),
            fx=ReferenceFx(), fx_error=None, market_clock=Mock(return_value=SimpleNamespace(is_open=False)),
            latest_completed_session=Mock(return_value=EXPECTED),
            maximum_entry_price=lambda ask: ask * 1.0005, open_orders=Mock(return_value=[]), _client=Mock(),
        )
        self.broker_factory = stack.enter_context(patch('trading_bot.live_trading.AlpacaLiveBroker', return_value=self.broker))
        self.provider = stack.enter_context(patch('trading_bot.live_trading.AlpacaLiveMarketDataProvider')).return_value
        self.provider.load.return_value = {'AAPL': history('AAPL', 50), 'MSFT': history('MSFT', 80)}
        self.provider.latest_entry_prices.side_effect = lambda symbols: {symbol: 50 for symbol in symbols}
        stack.enter_context(patch('trading_bot.live_trading.candidate_session', return_value=SimpleNamespace(symbols=['AAPL', 'MSFT'])))
        self.sync = stack.enter_context(patch('trading_bot.reconcile.sync_broker_orders'))
        stack.enter_context(patch('trading_bot.reconcile.reconcile_positions', return_value=ReconciliationResult(True, [])))

    def assert_read_only_and_unproven(self, report):
        self.assertFalse(report['broker_writes_enabled'])
        self.assertIs(report['strategy_profitability_proven'], False)
        self.broker_factory.assert_called_once_with(self.policy, submit_enabled=False)
        self.broker._client.submit_order.assert_not_called()
        self.broker._client.replace_order_by_id.assert_not_called()
        self.broker._client.cancel_order_by_id.assert_not_called()
        persisted = json.loads((self.directory / 'live-check.json').read_text(encoding='utf-8'))
        self.assertEqual(persisted['technical_checks_passed'], report['technical_checks_passed'])

    def test_completed_195_bar_histories_pass_closed_market_with_quotes_visibly_pending(self):
        report = check(self.policy)
        self.assertTrue(report['technical_checks_passed'])
        self.assertEqual(report['market_data_check']['expected_completed_session'], EXPECTED.isoformat())
        self.assertEqual(report['market_data_check']['missing_or_insufficient_history'], [])
        self.assertEqual(report['market_data_check']['affordable_at_previous_close'], ['AAPL', 'MSFT'])
        self.assertFalse(report['market_data_check']['fresh_entry_quotes_checked'])
        self.provider.latest_entry_prices.assert_not_called()
        self.assert_read_only_and_unproven(report)

    def test_market_data_permission_failure_blocks_readiness(self):
        self.provider.load.side_effect = APIError('{"code":40310000,"message":"market data permission denied"}')
        report = check(self.policy)
        self.assertFalse(report['technical_checks_passed'])
        self.assertIn('permission denied', report['market_data_check']['error'])
        self.assert_read_only_and_unproven(report)

    def test_missing_latest_completed_bar_blocks_readiness(self):
        self.provider.load.return_value['AAPL'] = history('AAPL', 50, final_date=EXPECTED - timedelta(days=1))
        report = check(self.policy)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(report['market_data_check']['missing_or_insufficient_history'], ['AAPL'])
        self.assert_read_only_and_unproven(report)

    def test_insufficient_warmup_blocks_readiness_even_with_latest_bar(self):
        self.provider.load.return_value['AAPL'] = history('AAPL', 50, count=194)
        report = check(self.policy)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(report['market_data_check']['missing_or_insufficient_history'], ['AAPL'])
        self.assert_read_only_and_unproven(report)

    def test_no_capital_affordable_whole_share_blocks_readiness(self):
        self.provider.load.return_value = {'AAPL': history('AAPL', 101), 'MSFT': history('MSFT', 200)}
        report = check(self.policy)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(report['market_data_check']['entry_budget_usd'], 100)
        self.assertEqual(report['market_data_check']['affordable_at_previous_close'], [])
        self.assert_read_only_and_unproven(report)

    def test_current_quotes_checked_only_while_open_and_one_valid_symbol_is_enough(self):
        self.broker.market_clock.return_value.is_open = True

        def quote(symbols):
            if symbols == ['AAPL']:
                raise RuntimeError('AAPL quote is stale')
            return {'MSFT': 80}

        self.provider.latest_entry_prices.side_effect = quote
        report = check(self.policy)
        self.assertTrue(report['technical_checks_passed'])
        self.assertTrue(report['market_data_check']['fresh_entry_quotes_checked'])
        self.assertEqual(report['market_data_check']['quote_errors'], {'AAPL': 'AAPL quote is stale'})
        self.assertEqual(self.provider.latest_entry_prices.call_count, 2)
        self.assert_read_only_and_unproven(report)

    def test_no_valid_current_quotes_blocks_open_market_readiness(self):
        self.broker.market_clock.return_value.is_open = True
        self.provider.latest_entry_prices.side_effect = RuntimeError('quote is stale')
        report = check(self.policy)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(set(report['market_data_check']['quote_errors']), {'AAPL', 'MSFT'})
        self.assert_read_only_and_unproven(report)

    def test_fresh_asks_above_entry_budget_block_readiness_despite_affordable_previous_closes(self):
        self.broker.market_clock.return_value.is_open = True
        self.provider.latest_entry_prices.side_effect = lambda symbols: {symbol: 101 for symbol in symbols}
        report = check(self.policy)
        self.assertEqual(report['market_data_check']['affordable_at_previous_close'], ['AAPL', 'MSFT'])
        self.assertFalse(report['technical_checks_passed'])
        self.assertTrue(report['market_data_check']['fresh_entry_quotes_checked'])
        self.assert_read_only_and_unproven(report)

    def test_one_affordable_current_ask_passes_even_if_other_previous_close_is_now_unaffordable(self):
        self.broker.market_clock.return_value.is_open = True
        self.provider.latest_entry_prices.side_effect = lambda symbols: {symbol: (101 if symbol == 'AAPL' else 80)
                                                                      for symbol in symbols}
        report = check(self.policy)
        self.assertEqual(report['market_data_check']['affordable_at_previous_close'], ['AAPL', 'MSFT'])
        self.assertTrue(report['technical_checks_passed'])
        self.assertTrue(report['market_data_check']['fresh_entry_quotes_checked'])
        self.assert_read_only_and_unproven(report)

    def test_unverified_fx_cannot_pass_preflight_despite_valid_history(self):
        self.broker.fx_error = 'reference is stale'
        report = check(self.policy)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(report['fx_warning'], 'reference is stale')
        self.assert_read_only_and_unproven(report)


if __name__ == '__main__':
    unittest.main()
