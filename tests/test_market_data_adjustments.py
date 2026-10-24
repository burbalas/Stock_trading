from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.data.enums import Adjustment
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from trading_bot.alpaca_integration import AlpacaMarketDataProvider


class MarketDataAdjustmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.get_stock_bars.return_value = SimpleNamespace(data={})
        self.request = Mock(side_effect=StockBarsRequest)
        modules = {
            "StockHistoricalDataClient": Mock(return_value=self.client),
            "StockBarsRequest": self.request,
            "StockLatestQuoteRequest": Mock(),
            "TimeFrame": TimeFrame,
        }
        self.enterContext(patch("trading_bot.alpaca_integration._load_alpaca_modules", return_value=modules))
        self.enterContext(patch("trading_bot.alpaca_integration._alpaca_keys", return_value=("test-key", "test-secret")))
        self.env_loader = self.enterContext(patch("trading_bot.alpaca_integration._load_local_env"))

    def tearDown(self) -> None:
        self.env_loader.assert_not_called()

    def test_default_requests_split_adjusted_history_and_preserves_returned_prices(self) -> None:
        def bar(day, price):
            return SimpleNamespace(
                timestamp=datetime(2026, 9, day, tzinfo=timezone.utc),
                open=price, high=price + 1, low=price - 1, close=price, volume=1000,
            )

        # These are the server's adjusted values, already on a comparable basis
        # across a split; the provider must not apply an additional adjustment.
        self.client.get_stock_bars.return_value = SimpleNamespace(
            data={"AAPL": [bar(15, 102), bar(14, 101), bar(11, 100)]}
        )
        provider = AlpacaMarketDataProvider(180, "iex", date(2026, 9, 15))
        data = provider.load(["aapl"])

        self.assertEqual(self.request.call_args.kwargs["adjustment"], "split")
        actual_request = self.client.get_stock_bars.call_args.args[0]
        self.assertEqual(actual_request.adjustment, Adjustment.SPLIT)
        self.assertEqual(actual_request.symbol_or_symbols, ["AAPL"])
        self.assertEqual([row.date for row in data["AAPL"]], [date(2026, 9, 11), date(2026, 9, 14)])
        self.assertEqual([row.close for row in data["AAPL"]], [100, 101])

    def test_explicit_adjustment_overrides_reach_the_sdk_request(self) -> None:
        for adjustment in ("raw", "dividend", "all"):
            with self.subTest(adjustment=adjustment):
                provider = AlpacaMarketDataProvider(feed="sip", adjustment=adjustment)
                provider.load(["SPY"])
                self.assertEqual(self.request.call_args.kwargs["adjustment"], adjustment)
                actual_request = self.client.get_stock_bars.call_args.args[0]
                self.assertEqual(actual_request.adjustment, Adjustment(adjustment))

    def test_invalid_adjustment_fails_before_loading_credentials(self) -> None:
        with patch("trading_bot.alpaca_integration._alpaca_keys") as keys:
            for adjustment in (None, "", "invalid"):
                with self.subTest(adjustment=adjustment), self.assertRaisesRegex(ValueError, "adjustment must"):
                    AlpacaMarketDataProvider(adjustment=adjustment)
            keys.assert_not_called()


if __name__ == "__main__":
    unittest.main()
