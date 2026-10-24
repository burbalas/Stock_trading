from datetime import date, timedelta
from dataclasses import replace
import unittest

from trading_bot.config import BotConfig
from trading_bot.models import Candle, Side, Signal
from trading_bot.profitability_study import PassThroughAdvisor, passive, run_candidate, select_candidate


class BuyStrategy:
    def generate(self, symbol, history):
        return Signal(symbol, Side.BUY, .9, 'test')


class ProfitabilityStudyTests(unittest.TestCase):
    def test_selection_ignores_final_results_and_stress_rows(self):
        rows = []
        for name, growth in [('legacy_ensemble_3pct_trail', 2), ('candidate', 6)]:
            for period in ('development', 'validation'):
                rows.append(dict(candidate=name, period=period, slippage_bps=5,
                                 return_pct=growth, cagr_pct=growth, max_drawdown_pct=-3))
        expected = select_candidate(rows, .15)
        self.assertEqual(expected[0], 'candidate')
        rows += [dict(row, period='final', return_pct=-99, cagr_pct=-99) for row in rows]
        rows += [dict(row, slippage_bps=20, return_pct=-99, cagr_pct=-99) for row in rows]
        self.assertEqual(select_candidate(rows, .15), expected)

    def test_selection_returns_none_when_risk_limit_is_breached(self):
        rows = [dict(candidate=name, period=period, slippage_bps=5, return_pct=growth,
                     cagr_pct=growth, max_drawdown_pct=drawdown)
                for name, growth, drawdown in [('legacy_ensemble_3pct_trail', 2, -3), ('candidate', 6, -16)]
                for period in ('development', 'validation')]
        self.assertEqual(select_candidate(rows, .15), (None, {}))

    def test_passive_uses_second_session_open_and_charges_entry_cost(self):
        start = date(2020, 1, 1)
        bars = [Candle(start, 'A', 100, 100, 100, 100, 1000),
                Candle(start + timedelta(days=1), 'A', 200, 220, 200, 220, 1000)]
        result = passive({'A': bars}, ['A'], (start, start + timedelta(days=1)), .4, 100)
        expected_equity = 60000 + 40000 / 202 * 220
        self.assertAlmostEqual(result['final_equity'], expected_equity)
        self.assertAlmostEqual(result['return_pct'], (expected_equity / 100000 - 1) * 100)
        self.assertEqual(result['trades'], 1)
        self.assertEqual(result['traded_notional_multiple'], .4)

    def test_future_bars_cannot_change_earlier_candidate_result(self):
        start = date(2020, 1, 1)
        bars = [Candle(start + timedelta(days=i), 'A', 100 + i, 100 + i, 100 + i, 100 + i, 1000)
                for i in range(15)]
        config = BotConfig(symbols=['A'], stop_loss_pct=.9, trailing_stop_pct=.9)
        specification = (config, BuyStrategy(), PassThroughAdvisor())
        period = (start + timedelta(days=5), start + timedelta(days=10))
        original = run_candidate({'A': bars}, specification, period, 5)
        changed = [replace(bar, open=1, high=1, low=1, close=1) if bar.date > period[1] else bar for bar in bars]
        self.assertEqual(run_candidate({'A': changed}, specification, period, 5), original)
        self.assertEqual(original['start'], period[0].isoformat())


if __name__ == '__main__':
    unittest.main()
