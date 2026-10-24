import json
import math
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from trading_bot.evidence_statistics import analyze_paired_equity


def curve(returns, *, capital=2500.0, exposure=.4):
    equity = capital
    result = [{'date': date(2020, 1, 1), 'equity': equity, 'positions_value': equity * exposure}]
    for index, daily_return in enumerate(returns, 1):
        equity *= 1 + daily_return
        result.append({'date': date(2020, 1, 1) + timedelta(days=index),
                       'equity': equity, 'positions_value': equity * exposure})
    return result


class EvidenceStatisticsTests(unittest.TestCase):
    def analyze(self, candidate, benchmark, **kwargs):
        return analyze_paired_equity(candidate, benchmark, bootstrap_samples=200,
                                     costs_included=kwargs.pop('costs_included', True), **kwargs)

    def test_identical_paired_paths_have_exact_zero_excess_and_intervals(self):
        points = curve([.01, -.02, .015, -.004] * 70)
        result = self.analyze(points, points)
        self.assertEqual(result['relative_wealth_gain_pct'], 0)
        self.assertEqual(result['mean_daily_log_excess_bps'], 0)
        for sensitivity in result['bootstrap']['sensitivities']:
            self.assertEqual(sensitivity['relative_wealth_gain_pct_ci'], {'lower': 0, 'upper': 0})
        self.assertFalse(result['evidence_screen_passed'])

    def test_relative_wealth_is_not_return_percentage_point_difference(self):
        result = self.analyze(curve([.1, .1]), curve([.05, .05]))
        self.assertAlmostEqual(result['candidate_return_pct'], 21)
        self.assertAlmostEqual(result['benchmark_return_pct'], 10.25)
        self.assertAlmostEqual(result['total_return_difference_percentage_points'], 10.75)
        self.assertAlmostEqual(result['relative_wealth_gain_pct'], (1.21 / 1.1025 - 1) * 100)
        self.assertEqual(result['return_sessions'], 2)

    def test_constant_excess_has_known_compound_confidence_interval(self):
        result = self.analyze(curve([.001] * 260), curve([0] * 260))
        expected = (1.001 ** 260 - 1) * 100
        self.assertTrue(result['evidence_screen_passed'])
        for sensitivity in result['bootstrap']['sensitivities']:
            interval = sensitivity['relative_wealth_gain_pct_ci']
            self.assertAlmostEqual(interval['lower'], expected, places=8)
            self.assertAlmostEqual(interval['upper'], expected, places=8)
            self.assertAlmostEqual(sensitivity['mean_daily_simple_excess_bps_ci']['lower'], 10, places=8)

    def test_equal_seed_is_reproducible_and_different_seed_changes_intervals(self):
        candidate = curve([.01, .02, -.005, .003, -.03, .04, .002] * 40)
        benchmark = curve([.003, .006, -.002, .001, -.01, .01, .001] * 40)
        first = self.analyze(candidate, benchmark)
        second = self.analyze(candidate, benchmark)
        third = self.analyze(candidate, benchmark, seed=1728)
        self.assertEqual(first, second)
        self.assertNotEqual(first['bootstrap']['sensitivities'], third['bootstrap']['sensitivities'])

    def test_short_sample_is_descriptive_and_cannot_pass_checkpoint(self):
        result = self.analyze(curve([.01] * 27), curve([0] * 27))
        self.assertFalse(result['evidence_screen_passed'])
        self.assertEqual(result['return_sessions'], 27)
        self.assertEqual(len(result['bootstrap']['sensitivities']), 2)
        self.assertTrue(any('252' in warning for warning in result['diagnostics']))
        self.assertTrue(any('ten blocks' in warning for warning in result['diagnostics']))

    def test_missing_or_shifted_dates_are_rejected_instead_of_intersected(self):
        candidate = curve([.01] * 25)
        benchmark = curve([.005] * 25)
        with self.assertRaisesRegex(ValueError, 'exactly matching session dates'):
            self.analyze(candidate, benchmark[:5] + benchmark[6:])
        benchmark[-1]['date'] += timedelta(days=1)
        with self.assertRaisesRegex(ValueError, 'exactly matching session dates'):
            self.analyze(candidate, benchmark)

    def test_duplicate_unsorted_and_invalid_session_dates_are_rejected(self):
        for replacement in [date(2020, 1, 1), 'not-a-date', None]:
            candidate = curve([.01] * 25)
            candidate[1]['date'] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.analyze(candidate, curve([.005] * 25))

    def test_bad_equity_and_complete_capital_loss_are_rejected(self):
        for equity in [0, -1, math.nan, math.inf, -math.inf, True]:
            candidate = curve([.01] * 25)
            candidate[1]['equity'] = equity
            with self.subTest(equity=equity), self.assertRaises(ValueError):
                self.analyze(candidate, curve([.005] * 25))

    def test_same_capital_required_even_for_tiny_amounts(self):
        for capital in [2501, 1e-11]:
            with self.subTest(capital=capital), self.assertRaisesRegex(ValueError, 'same capital'):
                self.analyze(curve([.01] * 25, capital=1e-9 if capital < 1 else 2500),
                             curve([.005] * 25, capital=capital))

    def test_drawdown_uses_running_peak_and_has_negative_sign(self):
        candidate = curve([.1, -.2, .25])
        result = self.analyze(candidate, curve([0] * 3))
        self.assertAlmostEqual(result['candidate_max_drawdown_pct'], -20)
        self.assertEqual(result['benchmark_max_drawdown_pct'], 0)
        self.assertAlmostEqual(result['candidate_average_exposure_pct'], 40)

    def test_chronological_half_reversal_blocks_positive_full_sample(self):
        result = self.analyze(curve([.003] * 130 + [-.001] * 130), curve([0] * 260))
        self.assertGreater(result['relative_wealth_gain_pct'], 0)
        self.assertGreater(result['chronological_halves'][0]['relative_wealth_gain_pct'], 0)
        self.assertLess(result['chronological_halves'][1]['relative_wealth_gain_pct'], 0)
        self.assertFalse(result['evidence_screen_passed'])
        self.assertTrue(any('both chronological halves' in warning for warning in result['diagnostics']))

    def test_growing_strategy_can_underperform_benchmark(self):
        result = self.analyze(curve([.001] * 260), curve([.002] * 260))
        self.assertGreater(result['candidate_return_pct'], 0)
        self.assertLess(result['relative_wealth_gain_pct'], 0)
        self.assertFalse(result['evidence_screen_passed'])

    def test_missing_cost_assertion_cannot_pass(self):
        for assertion in [None, False]:
            result = self.analyze(curve([.001] * 260), curve([0] * 260), costs_included=assertion)
            self.assertFalse(result['evidence_screen_passed'])
            self.assertTrue(any('Net-of-cost' in warning for warning in result['diagnostics']))

    def test_objects_and_iso_dates_supported_and_unknown_exposure_stays_unknown(self):
        candidate = [SimpleNamespace(date=point['date'].isoformat(), equity=point['equity'])
                     for point in curve([.01] * 25)]
        result = self.analyze(candidate, curve([0] * 25))
        self.assertIsNone(result['candidate_average_exposure_pct'])
        self.assertEqual(result['benchmark_average_exposure_pct'], 40)
        json.dumps(result, allow_nan=False)

    def test_invalid_controls_and_invested_values_are_rejected(self):
        candidate, benchmark = curve([.01] * 25), curve([0] * 25)
        for parameters in [{'block_lengths': (0, 20)}, {'block_lengths': (5, 5)},
                           {'minimum_sessions': 0}, {'confidence': 1}, {'currency': 'eur'},
                           {'seed': True}, {'costs_included': 'yes'}]:
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                self.analyze(candidate, benchmark, **parameters)
        candidate[2]['positions_value'] = math.nan
        with self.assertRaisesRegex(ValueError, 'invested value'):
            self.analyze(candidate, benchmark)

    def test_too_short_for_blocks_skips_without_inventing_observations(self):
        result = self.analyze(curve([.01, .02]), curve([0, 0]))
        self.assertEqual(result['bootstrap']['sensitivities'], [])
        self.assertFalse(result['evidence_screen_passed'])
        self.assertEqual(result['return_sessions'], 2)


if __name__ == '__main__':
    unittest.main()
