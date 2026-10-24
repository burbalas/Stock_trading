from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_bot.config import BotConfig
from trading_bot.data import StaticMarketDataProvider
from trading_bot.evidence_update import registered_trial, update_trial
from trading_bot.models import Candle, PortfolioSnapshot, Side, Signal
from trading_bot.profitability_evidence import normalized_ledger, audit_adjustments, euro_curve, summarize
from trading_bot.profitability_evidence import run_study
from trading_bot.profitability_study import STOCKS, ETFS
from trading_bot.research_accounting import CorporateActionLedger, CorporateActionResearchEngine, DividendEvent


FIRST = date(2026, 9, 15)


def bars(values):
    return [Candle(FIRST + timedelta(days=index), 'AAA', price, price, price, price, 100)
            for index, price in enumerate(values)]


class BuyOnce:
    def generate(self, symbol, history):
        return Signal(symbol, Side.BUY if len(history) == 1 else Side.HOLD, 1, 'fixture')


class Identity:
    def review(self, signal, history):
        return signal


class ProfitabilityEvidenceTests(unittest.TestCase):
    def modes(self):
        return {'raw': {'AAA': bars([100, 100, 99])}, 'split': {'AAA': bars([100, 100, 99])},
                'all': {'AAA': bars([99, 99, 99])}}

    def dividend(self, **overrides):
        return {'symbol': 'AAA', 'ex_date': '2026-09-17', 'payable_date': '2026-10-30',
                'process_date': '2026-10-30', 'rate': 1, **overrides}

    def test_future_payment_retains_current_entitlement_and_reconciles(self):
        modes = self.modes()
        ledger = normalized_ledger({'cash_dividends': [self.dividend()]}, modes['raw'],
                                   start=FIRST, end=FIRST + timedelta(days=2))
        self.assertEqual(len(ledger.dividends), 1)
        self.assertGreater(ledger.dividends[0].pay_date, ledger.coverage_end)
        audit = audit_adjustments(modes, ledger, start=FIRST, end=ledger.coverage_end)
        self.assertAlmostEqual(audit['dividend_factor_checks'][0]['rounded_bar_implied_amount'], 1)

    def test_missing_cash_action_blocks_instead_of_assuming_no_dividend(self):
        modes = self.modes()
        ledger = CorporateActionLedger(FIRST, FIRST + timedelta(days=2), 'fixture', True)
        with self.assertRaisesRegex(ValueError, 'Unresolved cash adjustment'):
            audit_adjustments(modes, ledger, start=FIRST, end=ledger.coverage_end)

    def test_missing_payment_date_blocks(self):
        with self.assertRaisesRegex(ValueError, 'payment date'):
            normalized_ledger({'cash_dividends': [self.dividend(payable_date=None)]}, self.modes()['raw'],
                              start=FIRST, end=FIRST + timedelta(days=2))

    def test_missing_ex_date_cannot_be_replaced_by_payment_process_date(self):
        with self.assertRaisesRegex(ValueError, 'economic effective/ex-date'):
            normalized_ledger({'cash_dividends': [self.dividend(ex_date=None)]}, self.modes()['raw'],
                              start=FIRST, end=FIRST + timedelta(days=2))

    def test_future_ex_date_never_creates_an_early_asset(self):
        ledger = normalized_ledger({'cash_dividends': [self.dividend(ex_date='2026-09-30')]},
                                   self.modes()['raw'], start=FIRST, end=FIRST + timedelta(days=2))
        self.assertEqual(ledger.dividends, ())

    def test_unsupported_effective_merger_blocks(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported evaluation-period action'):
            normalized_ledger({'cash_mergers': [{'symbol': 'AAA', 'effective_date': '2026-09-16'}]},
                              self.modes()['raw'], start=FIRST, end=FIRST + timedelta(days=2))

    def test_actual_merger_and_name_change_roles_cannot_be_silently_skipped(self):
        for kind, row in (
            ('cash_mergers', {'acquiree_symbol': 'OLD', 'acquirer_symbol': 'AAA', 'effective_date': '2026-09-16'}),
            ('stock_mergers', {'acquiree_symbol': 'AAA', 'acquirer_symbol': 'NEW', 'effective_date': '2026-09-16'}),
            ('name_changes', {'old_symbol': 'OLD', 'new_symbol': 'AAA', 'process_date': '2026-09-16'}),
        ):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'Unsupported evaluation-period action'):
                normalized_ledger({kind: [row]}, self.modes()['raw'], start=FIRST, end=FIRST + timedelta(days=2))

    def test_missing_split_fails_even_with_no_evaluation_splits(self):
        modes = {'raw': {'AAA': bars([100, 50, 50])}, 'split': {'AAA': bars([50, 50, 50])},
                 'all': {'AAA': bars([50, 50, 50])}}
        ledger = CorporateActionLedger(FIRST, FIRST + timedelta(days=2), 'fixture', True)
        with self.assertRaisesRegex(ValueError, 'Unresolved split'):
            audit_adjustments(modes, ledger, start=FIRST + timedelta(days=1), end=ledger.coverage_end)

    def test_fx_marks_use_only_past_reference_and_show_cash_currency_gain(self):
        curve = [PortfolioSnapshot(FIRST, 100, 0, 100), PortfolioSnapshot(FIRST + timedelta(days=1), 100, 0, 100)]
        fx = {FIRST: 1.25, FIRST + timedelta(days=1): 1, FIRST + timedelta(days=2): .5}
        euro = euro_curve(curve, fx)
        self.assertEqual([row.equity for row in euro], [80, 100])
        with self.assertRaises(ValueError):
            euro_curve(curve, {FIRST + timedelta(days=1): 1})

    def test_unpaid_dividend_is_asset_and_terminal_exit_cost_is_separate(self):
        raw = {'AAA': bars([100, 100, 99])}
        ledger = CorporateActionLedger(FIRST, FIRST + timedelta(days=2), 'fixture', True,
            dividends=(DividendEvent('AAA', FIRST + timedelta(days=2), date(2026, 10, 30), 1),))
        config = BotConfig(symbols=['AAA'], starting_cash=1000, max_order_pct=.5,
            max_position_pct=.5, target_cash_pct=0, min_trade_value=0, slippage_bps=0,
            stop_loss_pct=.9, trailing_stop_pct=.9)
        result = CorporateActionResearchEngine(config, StaticMarketDataProvider(raw), BuyOnce(), Identity(), ledger).run()
        summary = summarize(result, raw, ledger, {FIRST: 1}, slippage=20, commission=0)
        self.assertEqual(summary['ending_held_shares'], {'AAA': 5})
        self.assertEqual(summary['unpaid_dividend_assets_usd'], 5)
        self.assertEqual(summary['pnl_usd'], 0)
        self.assertAlmostEqual(summary['terminal_exit_cost_usd'], .99)
        self.assertLess(summary['terminal_liquidation_sensitivity_return_usd_pct'], 0)
        self.assertEqual(summary['closed_round_trips'], 0)

    def test_first_forward_baseline_runs_all_cohorts_without_inventing_returns(self):
        # Synthetic one-session fixture, never stored in the real trial. The
        # first eligible session must work before any return or closure exists.
        raw = {symbol: [Candle(FIRST, symbol, 100, 100, 100, 100, 100)]
               for symbol in STOCKS + ETFS}
        modes = {mode: raw for mode in ('raw', 'split', 'all')}
        with tempfile.TemporaryDirectory() as directory, \
             patch('trading_bot.profitability_evidence.load_archive', return_value=({}, modes, {}, {FIRST: 1}, None)), \
             patch('trading_bot.profitability_evidence.digest', return_value='a' * 64):
            result = run_study(Path(directory) / 'archive', Path(directory) / 'study', start=FIRST, end=FIRST)
        self.assertEqual(len(result['rows']), 24)
        self.assertTrue(all(row['return_sessions'] == 0 for row in result['rows']))
        self.assertTrue(all(row['return_usd_pct'] == 0 for row in result['rows']))
        self.assertTrue(all(row['entries'] == 0 for row in result['rows']))
        self.assertTrue(all(not row['evidence_screen_passed'] for row in result['comparisons']))


class EvidenceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.py'
        self.source.write_text('frozen', encoding='utf-8')
        self.directory = self.root / 'trial'
        self.now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        self.freeze = patch('trading_bot.evidence_update.frozen_files', return_value={'source': self.source})
        self.freeze.start()
        self.addCleanup(self.freeze.stop)
        self.addCleanup(self.temp.cleanup)

    @patch('trading_bot.evidence_update.archive_research')
    def test_before_first_completed_session_has_zero_rows_and_no_network(self, archive):
        status = update_trial(directory=self.directory, now=self.now)
        archive.assert_not_called()
        self.assertEqual(status['status'], 'NO_FORWARD_DATA')
        self.assertTrue(all(row['complete_market_sessions'] == 0 for row in status['cohorts']))
        self.assertFalse(status['promotion_authorized'])

    @patch('trading_bot.evidence_update.archive_research')
    def test_frozen_change_blocks_before_network_or_append(self, archive):
        registered_trial(self.directory, now=self.now)
        self.source.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Frozen trial files changed'):
            update_trial(directory=self.directory, now=self.now)
        archive.assert_not_called()

    def test_missing_old_registration_cannot_claim_a_retroactive_forward_trial(self):
        with self.assertRaisesRegex(RuntimeError, 'new documented trial'):
            registered_trial(self.directory, now=datetime(2026, 10, 24, 12, tzinfo=timezone.utc))


if __name__ == '__main__':
    unittest.main()
