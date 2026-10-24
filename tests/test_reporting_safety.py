import unittest
from datetime import date

from trading_bot.cli import _reason_ingredients, _strategy_trade_rows
from trading_bot.models import Fill, Side


class ReportingSafetyTests(unittest.TestCase):
    def test_recovered_same_day_sell_before_buy_keeps_correct_cash_profit(self):
        day = date(2026, 7, 27)
        fills = [Fill(day, "MSFT", Side.SELL, 1, 389.91, 389.91, "recovered"),
                 Fill(day, "MSFT", Side.BUY, 1, 390.05, 390.05, "recovered")]
        rows = _strategy_trade_rows(fills, {})
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["pl"], -.14)

    def test_unmatched_sell_cannot_silently_disappear_from_performance(self):
        fill = Fill(date(2026, 7, 27), "MSFT", Side.SELL, 1, 100, 100, "recovered")
        with self.assertRaisesRegex(RuntimeError, "opening fills are missing"):
            _strategy_trade_rows([fill], {})

    def test_unvalued_open_lot_cannot_be_reported_as_a_flat_account(self):
        fill = Fill(date(2026, 7, 27), "MSFT", Side.BUY, 1, 100, 100, "recovered")
        with self.assertRaisesRegex(RuntimeError, "current broker position/mark is missing"):
            _strategy_trade_rows([fill], {})

    def test_broker_uuid_is_not_presented_as_a_signal_ingredient(self):
        groups = _reason_ingredients("confirmed Alpaca execution: generated-order-uuid")
        self.assertEqual(groups, ["broker-recovered fill; original signal attribution unavailable"])
