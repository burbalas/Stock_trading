from datetime import date, timedelta
import unittest

from trading_bot.engine import RunResult
from trading_bot.models import Candle, PortfolioSnapshot, Side
from trading_bot.research_accounting import CorporateActionLedger, DividendEvent, SplitEvent
from trading_bot.research_benchmarks import (
    average_invested_fraction, ex_post_exposure_reference,
    run_cash_reference, run_passive_raw,
)


FIRST = date(2020, 1, 6)


def day(index):
    return FIRST + timedelta(days=index)


def rows(prices, symbol="AAA"):
    return [Candle(day(index), symbol, price, price, price, price, 1000)
            for index, price in enumerate(prices)]


class ResearchBenchmarkTests(unittest.TestCase):
    def run_case(self, prices, *, fraction=1, cash=1000, symbols=None, splits=(), dividends=(),
                 start=FIRST, slippage=0, commission=0, withholding=0):
        data = prices if isinstance(prices, dict) else {"AAA": rows(prices)}
        dates = [bar.date for bars in data.values() for bar in bars]
        ledger = CorporateActionLedger(min(dates), max(dates), "verified fixture", True, splits, dividends)
        return run_passive_raw(data, ledger, start=start, starting_cash=cash,
                               symbols=symbols or list(data), fraction=fraction,
                               slippage_bps=slippage, commission=commission, withholding=withholding)

    def test_first_evaluation_close_plans_fill_at_next_raw_open(self):
        result = self.run_case([100, 110, 120])
        self.assertEqual(result.snapshots[0].cash, 1000)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].date, day(1))
        self.assertEqual(result.fills[0].price, 110)
        self.assertEqual(result.fills[0].quantity, 9)
        self.assertEqual(result.final_equity, 1090)

    def test_downward_gap_does_not_increase_already_planned_quantity(self):
        result = self.run_case([100, 50, 60])
        self.assertEqual(result.fills[0].quantity, 10)
        self.assertEqual(result.snapshots[1].cash, 500)
        self.assertEqual(result.final_equity, 1100)

    def test_warmup_does_not_create_entry_before_evaluation_period(self):
        result = self.run_case([100, 200, 100, 100], start=day(2))
        self.assertEqual([item.date for item in result.snapshots], [day(2), day(3)])
        self.assertEqual(result.fills[0].date, day(3))
        self.assertEqual(result.fills[0].quantity, 10)

    def test_fixed_fraction_and_whole_share_rounding_leave_cash(self):
        result = self.run_case([90, 90, 90], fraction=.4)
        self.assertEqual(result.fills[0].quantity, 4)
        self.assertEqual(result.snapshots[-1].cash, 640)
        self.assertLessEqual(result.snapshots[-1].positions_value, 400)
        self.assertEqual(result.final_equity, 1000)

    def test_equal_asset_allocations_with_costs_never_overdraw_cash(self):
        result = self.run_case({"AAA": rows([100, 100, 100]), "BBB": rows([80, 80, 80], "BBB")},
                               slippage=10, commission=1)
        self.assertEqual(len(result.fills), 2)
        self.assertEqual({item.symbol: item.quantity for item in result.fills}, {"AAA": 4, "BBB": 6})
        self.assertGreaterEqual(result.snapshots[-1].cash, 0)
        self.assertAlmostEqual(result.final_equity, 997.12)

    def test_unaffordable_asset_is_not_purchased_after_price_falls(self):
        result = self.run_case([2000, 2000, 10, 10])
        self.assertEqual(result.fills, [])
        self.assertEqual(result.final_equity, 1000)

    def test_passive_does_not_exit_large_drawdown_or_trailing_reversal(self):
        result = self.run_case([100, 100, 200, 20, 30])
        self.assertEqual([item.side for item in result.fills], [Side.BUY])
        self.assertEqual(result.final_equity, 300)
        self.assertIn("effectively disabled", " ".join(result.assumptions))

    def test_cash_dividend_uses_entitlement_and_is_not_reinvested(self):
        result = self.run_case([100, 100, 90, 90, 90],
            dividends=(DividendEvent("AAA", day(2), day(3), 10),))
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.corporate_cash_flows[0].amount, 100)
        self.assertEqual(result.snapshots[-1].cash, 100)
        self.assertEqual(result.final_equity, 1000)

    def test_benchmark_buyer_on_ex_date_gets_no_entitlement(self):
        result = self.run_case([100, 90, 90], dividends=(DividendEvent("AAA", day(1), day(2), 10),))
        self.assertEqual(result.corporate_cash_flows, ())
        self.assertEqual(result.unpaid_dividends, ())

    def test_split_transforms_whole_holdings_without_inventing_gain(self):
        result = self.run_case([100, 100, 50, 60], splits=(SplitEvent("AAA", day(2), 2, 1),))
        self.assertEqual(result.fills[0].quantity, 10)
        self.assertEqual(result.snapshots[2].equity, 1000)
        self.assertEqual(result.final_equity, 1200)

    def test_split_on_entry_date_transforms_pending_whole_quantity(self):
        result = self.run_case([100, 50, 50], splits=(SplitEvent("AAA", day(1), 2, 1),))
        self.assertEqual(result.fills[0].quantity, 20)
        self.assertEqual(result.fills[0].price, 50)

    def test_ledger_is_filtered_to_requested_assets(self):
        data = {"AAA": rows([100, 100, 100]), "BBB": rows([100, 50, 50], "BBB")}
        result = self.run_case(data, symbols=["AAA"], splits=(SplitEvent("BBB", day(1), 2, 1),))
        self.assertEqual([item.symbol for item in result.fills], ["AAA"])
        self.assertEqual(result.final_equity, 1000)

    def test_cash_reference_matches_union_evaluation_grid_exactly(self):
        data = {"AAA": [rows([100, 100, 100])[index] for index in (0, 2)],
                "BBB": [rows([100, 100, 100], "BBB")[1]]}
        result = run_cash_reference(data, start=day(1), starting_cash=1234)
        self.assertEqual([item.date for item in result.snapshots], [day(1), day(2)])
        self.assertTrue(all(item.cash == item.equity == 1234 and item.positions_value == 0
                            for item in result.snapshots))
        self.assertEqual(result.fills, [])

    def test_exposure_reference_matches_observed_mean_exactly_and_has_no_fills(self):
        spy = self.run_case([100, 100, 200, 50, 120])
        reference = ex_post_exposure_reference(spy, candidate_average_exposure=.4)
        self.assertAlmostEqual(average_invested_fraction(reference), .4, places=12)
        self.assertEqual(reference.fills, [])
        self.assertGreater(reference.scale, 0)
        self.assertLess(reference.scale, 1)
        self.assertAlmostEqual(reference.final_equity, 1000 + reference.scale * 200)
        self.assertIn("ex-post", " ".join(reference.assumptions))
        self.assertIn("not an independently costed", " ".join(reference.assumptions))

    def test_exposure_reference_zero_is_cash_and_full_reproduces_spy_curve(self):
        spy = self.run_case([100, 100, 120])
        zero = ex_post_exposure_reference(spy, candidate_average_exposure=0)
        full = ex_post_exposure_reference(spy, candidate_average_exposure=average_invested_fraction(spy))
        self.assertTrue(all(abs(item.equity - 1000) < 1e-10 for item in zero.snapshots))
        self.assertAlmostEqual(full.final_equity, spy.final_equity)
        self.assertAlmostEqual(full.scale, 1)

    def test_exposure_reference_does_not_manufacture_leverage_or_unaffordable_shares(self):
        spy = self.run_case([100, 100, 120])
        with self.assertRaisesRegex(ValueError, "beyond"):
            ex_post_exposure_reference(spy, candidate_average_exposure=.9)
        unaffordable = self.run_case([2000, 2000, 2000])
        with self.assertRaisesRegex(ValueError, "unaffordable"):
            ex_post_exposure_reference(unaffordable, candidate_average_exposure=.1)
        zero = ex_post_exposure_reference(unaffordable, candidate_average_exposure=0)
        self.assertEqual(zero.scale, 0)

    def test_invalid_cash_fraction_exposure_and_initial_snapshot_fail(self):
        for value in (0, -1, float("nan"), True):
            with self.subTest(cash=value):
                with self.assertRaises(ValueError):
                    self.run_case([100, 100], cash=value)
        for value in (0, -1, 1.1, float("nan"), True):
            with self.subTest(fraction=value):
                with self.assertRaises(ValueError):
                    self.run_case([100, 100], fraction=value)
        spy = self.run_case([100, 100])
        for value in (-1, 1.1, float("nan"), True):
            with self.subTest(exposure=value):
                with self.assertRaises(ValueError):
                    ex_post_exposure_reference(spy, candidate_average_exposure=value)
        with self.assertRaises(ValueError):
            average_invested_fraction(RunResult([], []))
        with self.assertRaisesRegex(ValueError, "cash-only"):
            ex_post_exposure_reference(RunResult([PortfolioSnapshot(day(0), 500, 500, 1000)], []),
                                       candidate_average_exposure=.4)


if __name__ == "__main__":
    unittest.main()
