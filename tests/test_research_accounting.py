from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import unittest

from trading_bot.config import BotConfig
from trading_bot.data import StaticMarketDataProvider
from trading_bot.engine import TradingEngine
from trading_bot.models import Candle, Side, Signal
from trading_bot.research_accounting import (
    CorporateActionLedger, CorporateActionResearchEngine, DividendEvent, SplitEvent,
    split_adjusted_history,
)


FIRST = date(2020, 1, 6)


def day(index):
    return FIRST + timedelta(days=index)


def bar(index, price, *, low=None, high=None, symbol="AAA"):
    return Candle(day(index), symbol, price, high or price, low or price, price, 1000)


class IdentityAdvisor:
    def review(self, signal, history):
        return signal


class ScriptedStrategy:
    def __init__(self, actions=None):
        self.actions = actions or {day(0): Side.BUY}
        self.observed = {}

    def generate(self, symbol, history):
        self.observed[history[-1].date] = list(history)
        return Signal(symbol, self.actions.get(history[-1].date, Side.HOLD), 1.0, "script")


class ResearchAccountingTests(unittest.TestCase):
    def setUp(self):
        self.config = BotConfig(
            symbols=["AAA"], starting_cash=1000, max_order_pct=.5, max_position_pct=1,
            max_open_positions=1, max_daily_orders=1, min_trade_value=0, target_cash_pct=0,
            slippage_bps=0, commission_per_trade=0, stop_loss_pct=.5,
            trailing_stop_pct=.5, max_drawdown_stop_pct=.75,
        )

    def run_case(self, bars, *, splits=(), dividends=(), strategy=None, config=None, **kwargs):
        ledger = CorporateActionLedger(min(item.date for item in bars), max(item.date for item in bars),
                                       "verified fixture", True, splits, dividends)
        return CorporateActionResearchEngine(config or self.config,
            StaticMarketDataProvider({"AAA": bars}), strategy or ScriptedStrategy(),
            IdentityAdvisor(), ledger, **kwargs).run()

    def test_future_split_never_changes_earlier_signals_or_exposes_future_bar(self):
        rows = [bar(0, 100), bar(1, 110), bar(2, 55)]
        split = SplitEvent("AAA", day(2), 2, 1)
        before = split_adjusted_history(rows, (split,), day(1))
        self.assertEqual([item.close for item in before], [100, 110])
        after = split_adjusted_history(rows, (split,), day(2))
        self.assertEqual([item.close for item in after], [50, 55, 55])
        self.assertEqual([item.volume for item in after], [2000, 2000, 1000])
        self.assertEqual(rows[0].close, 100)

    def test_no_actions_matches_existing_engine_including_gap_and_costs(self):
        rows = [bar(0, 100), bar(1, 105), bar(2, 45), bar(3, 50)]
        config = replace(self.config, slippage_bps=12, commission_per_trade=1,
                         stop_loss_pct=.1, trailing_stop_pct=.1)
        result = self.run_case(rows, config=config)
        original = TradingEngine(config, StaticMarketDataProvider({"AAA": rows}),
                                 ScriptedStrategy(), IdentityAdvisor()).run()
        self.assertEqual(result.snapshots, original.snapshots)
        self.assertEqual(result.fills, original.fills)
        self.assertEqual(result.fills[-1].reason, "stop loss")
        self.assertAlmostEqual(result.fills[-1].price, 45 * (1 - .0012))

    def test_whole_share_split_changes_holdings_cost_and_stop_basis_without_profit(self):
        strategy = ScriptedStrategy({day(0): Side.BUY, day(2): Side.SELL})
        result = self.run_case([bar(0, 100), bar(1, 100), bar(2, 50), bar(3, 50)],
                               splits=(SplitEvent("AAA", day(2), 2, 1),), strategy=strategy)
        self.assertEqual([item.equity for item in result.snapshots], [1000] * 4)
        self.assertEqual([(fill.quantity, fill.price) for fill in result.fills], [(5, 100), (10, 50)])
        self.assertEqual([item.close for item in strategy.observed[day(1)]], [100, 100])
        self.assertEqual([item.close for item in strategy.observed[day(2)]], [50, 50, 50])

    def test_pending_buy_quantity_is_adjusted_on_split_open_using_raw_prices(self):
        result = self.run_case([bar(0, 100), bar(1, 50), bar(2, 50)],
                               splits=(SplitEvent("AAA", day(1), 2, 1),))
        self.assertEqual(result.fills[0].quantity, 10)
        self.assertEqual(result.fills[0].price, 50)
        self.assertEqual(result.final_equity, 1000)

    def test_dividend_accrues_ex_date_and_cash_waits_for_pay_date(self):
        result = self.run_case([bar(0, 100), bar(1, 100), bar(2, 90), bar(3, 90)],
            dividends=(DividendEvent("AAA", day(2), day(3), 10),))
        self.assertEqual([item.equity for item in result.snapshots], [1000] * 4)
        self.assertEqual([item.cash for item in result.snapshots], [1000, 500, 500, 550])
        self.assertEqual(result.daily_dividend_receivables, ((day(0), 0), (day(1), 0), (day(2), 50), (day(3), 0)))
        self.assertEqual(result.corporate_cash_flows[0].amount, 50)
        self.assertEqual(result.corporate_cash_flows[0].date, day(3))

    def test_buy_on_ex_date_does_not_earn_dividend(self):
        result = self.run_case([bar(0, 100), bar(1, 90), bar(2, 90)],
            dividends=(DividendEvent("AAA", day(1), day(2), 10),))
        self.assertEqual(result.corporate_cash_flows, ())
        self.assertEqual(result.daily_dividend_receivables[-1][1], 0)

    def test_seller_on_ex_date_keeps_entitlement(self):
        result = self.run_case([bar(0, 100), bar(1, 100), bar(2, 90), bar(3, 90)],
            strategy=ScriptedStrategy({day(0): Side.BUY, day(1): Side.SELL}),
            dividends=(DividendEvent("AAA", day(2), day(3), 10),))
        self.assertEqual(result.corporate_cash_flows[0].amount, 50)
        self.assertEqual(result.final_equity, 1000)

    def test_terminal_receivable_counts_as_equity_but_not_cash_and_withholding_applies(self):
        result = self.run_case([bar(0, 100), bar(1, 100), bar(2, 90)],
            dividends=(DividendEvent("AAA", day(2), day(8), 10),), dividend_withholding_pct=.15)
        self.assertEqual(result.snapshots[-1].cash, 500)
        self.assertEqual(result.unpaid_dividends[0].amount, 42.5)
        self.assertEqual(result.final_equity, 992.5)

    def test_fractional_split_rejects_without_verified_disposition_policy(self):
        with self.assertRaisesRegex(ValueError, "fractional shares"):
            self.run_case([bar(0, 100), bar(1, 100), bar(2, 200)],
                          splits=(SplitEvent("AAA", day(2), 1, 2),))

    def test_optional_cash_in_lieu_is_explicit_and_equity_preserving_before_costs(self):
        result = self.run_case([bar(0, 100), bar(1, 100), bar(2, 200)],
            splits=(SplitEvent("AAA", day(2), 1, 2),), fractional_split_policy="cash_at_open")
        self.assertEqual(result.final_equity, 1000)
        self.assertEqual(result.fills[-1].quantity, .5)
        self.assertEqual(result.corporate_cash_flows[0].kind, "assumed_cash_in_lieu")
        self.assertEqual(result.corporate_cash_flows[0].amount, 100)
        self.assertIn("not verified broker settlement", " ".join(result.assumptions))

    def test_incomplete_or_short_action_coverage_fails_before_simulation(self):
        provider = StaticMarketDataProvider({"AAA": [bar(0, 100), bar(1, 100)]})
        for ledger in (CorporateActionLedger(day(0), day(1), "unverified"),
                       CorporateActionLedger(day(1), day(1), "short", True)):
            with self.subTest(ledger=ledger):
                with self.assertRaises(ValueError):
                    CorporateActionResearchEngine(self.config, provider, ScriptedStrategy(),
                                                  IdentityAdvisor(), ledger).run()

    def test_duplicate_missing_bar_and_ambiguous_actions_fail_closed(self):
        cases = [
            ((SplitEvent("AAA", day(1), 2, 1),) * 2, ()),
            ((SplitEvent("AAA", day(1), 2, 1),), (DividendEvent("AAA", day(1), day(2), 1),)),
        ]
        for splits, dividends in cases:
            with self.subTest(splits=splits, dividends=dividends):
                with self.assertRaises(ValueError):
                    self.run_case([bar(0, 100), bar(1, 50), bar(2, 50)],
                                  splits=splits, dividends=dividends)
        ledger = CorporateActionLedger(day(0), day(2), "fixture", True,
                                       (SplitEvent("AAA", day(1), 2, 1),))
        with self.assertRaisesRegex(ValueError, "lacks its raw execution bar"):
            ledger.validate({"AAA": [bar(0, 100), bar(2, 50)]})

    def test_drawdown_halt_remains_latched_after_next_open_liquidation(self):
        result = self.run_case([bar(0, 100), bar(1, 100), bar(2, 70), bar(3, 60), bar(4, 150)],
            config=replace(self.config, max_drawdown_stop_pct=.1),
            strategy=ScriptedStrategy({day(index): Side.BUY for index in range(5)}))
        self.assertEqual([fill.side for fill in result.fills], [Side.BUY, Side.SELL])
        self.assertEqual(result.fills[-1].date, day(3))
        self.assertEqual(result.fills[-1].reason, "account drawdown stop")

    def test_invalid_action_numbers_and_currency_rejected(self):
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    SplitEvent("AAA", day(1), count, 1)
        for amount in (0, float("nan"), float("inf"), True):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    DividendEvent("AAA", day(1), day(2), amount)
        with self.assertRaises(ValueError):
            DividendEvent("AAA", day(1), day(2), 1, "EUR")


if __name__ == "__main__":
    unittest.main()
