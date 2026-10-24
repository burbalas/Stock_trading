import unittest
from copy import deepcopy
from datetime import date, timedelta

from trading_bot.advisor import HeuristicAdvisor
from trading_bot.config import BotConfig
from trading_bot.data import StaticMarketDataProvider
from trading_bot.engine import TradingEngine
from trading_bot.models import Candle, Side, Signal
from trading_bot.trend_research import MonthlyTrendStrategy


def candles(symbol, prices, end=date(2026, 2, 2)):
    dates = []
    current = end
    while len(dates) < len(prices):
        if current.weekday() < 5:
            dates.append(current)
        current -= timedelta(days=1)
    return [Candle(day, symbol, price, price, price, price, 100_000) for day, price in zip(reversed(dates), prices)]


class MonthlyTrendResearchTests(unittest.TestCase):
    def test_constructor_rejects_invalid_parameters(self):
        for kwargs in (
            {"trend_window": 0}, {"trend_window": True}, {"momentum_window": 2.5},
            {"skip_recent": -1}, {"skip_recent": 126}, {"top_k": 0}, {"top_k": False},
            {"confidence": float("nan")}, {"confidence": 1.1}, {"confidence": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MonthlyTrendStrategy(**kwargs)

    def test_monthly_review_repeats_eligible_buy_and_exits_failed_trend(self):
        strategy = MonthlyTrendStrategy(trend_window=3)
        february = candles("SPY", [100, 101, 103])
        signal = strategy.generate("SPY", february)
        self.assertIsInstance(signal, Signal)
        self.assertEqual(signal.side, Side.BUY)
        self.assertFalse(hasattr(signal, "quantity"))
        next_day = february + [Candle(date(2026, 2, 3), "SPY", 110, 110, 110, 110, 100_000)]
        self.assertEqual(strategy.generate("SPY", next_day).side, Side.HOLD)
        march = next_day + [Candle(date(2026, 3, 2), "SPY", 120, 120, 120, 120, 100_000)]
        self.assertEqual(strategy.generate("SPY", march).side, Side.BUY)
        april = march + [Candle(date(2026, 4, 1), "SPY", 90, 90, 90, 90, 100_000)]
        self.assertEqual(strategy.generate("SPY", april).side, Side.SELL)

    def test_missing_or_insufficient_history_cannot_enter_or_use_stale_rank(self):
        strategy = MonthlyTrendStrategy(trend_window=3)
        fresh = candles("SPY", [100, 101, 103])
        stale = candles("QQQ", [100, 110, 120], end=date(2026, 1, 31))
        histories = {"SPY": fresh, "QQQ": stale, "GLD": [], "IEF": candles("IEF", [100])}
        sides = {signal.symbol: signal.side for signal in strategy.generate_many(histories)}
        self.assertEqual(sides, {"GLD": Side.HOLD, "IEF": Side.HOLD, "QQQ": Side.HOLD, "SPY": Side.BUY})
        ranked = MonthlyTrendStrategy(trend_window=3, top_k=1)
        self.assertEqual(ranked.generate("SPY", fresh).side, Side.HOLD)

    def test_rank_ignores_skipped_recent_jump_and_breaks_ties_deterministically(self):
        strategy = MonthlyTrendStrategy(trend_window=2, momentum_window=6, skip_recent=2, top_k=1)
        spy = candles("SPY", [99, 100, 105, 110, 115, 120, 122, 124])
        qqq = candles("QQQ", [99, 100, 102, 105, 108, 110, 200, 220])
        sides = {signal.symbol: signal.side for signal in strategy.generate_many({"QQQ": qqq, "SPY": spy})}
        self.assertEqual(sides, {"QQQ": Side.SELL, "SPY": Side.BUY})
        same_qqq = candles("QQQ", [99, 100, 105, 110, 115, 120, 122, 124])
        sides = {signal.symbol: signal.side for signal in strategy.generate_many({"SPY": spy, "QQQ": same_qqq})}
        self.assertEqual(sides, {"QQQ": Side.BUY, "SPY": Side.SELL})

    def test_engine_executes_next_open_and_later_prices_do_not_change_first_fill(self):
        config = BotConfig(symbols=["SPY"], min_confidence=0.5, stop_loss_pct=0.9, trailing_stop_pct=0.9)
        history = candles("SPY", [100, 101, 104])
        first_fills = []
        for future_close in (50.0, 500.0):
            rows = history + [
                Candle(date(2026, 2, 3), "SPY", 107, 108, 106, 108, 100_000),
                Candle(date(2026, 2, 4), "SPY", future_close, future_close, future_close, future_close, 100_000),
            ]
            result = TradingEngine(
                config, StaticMarketDataProvider({"SPY": rows}),
                MonthlyTrendStrategy(trend_window=3), HeuristicAdvisor(),
            ).run()
            self.assertTrue(result.fills)
            first_fills.append(result.fills[0])
        self.assertEqual(first_fills[0], first_fills[1])
        self.assertEqual(first_fills[0].date, date(2026, 2, 3))
        self.assertAlmostEqual(first_fills[0].price, 107 * (1 + config.slippage_bps / 10_000))

    def test_repeated_calls_and_deepcopy_do_not_leak_evaluation_state(self):
        strategy = MonthlyTrendStrategy(trend_window=3)
        february = candles("SPY", [100, 101, 103])
        expected = strategy.generate("SPY", february)
        march = february + [Candle(date(2026, 3, 2), "SPY", 90, 90, 90, 90, 100_000)]
        strategy.generate("SPY", march)
        self.assertEqual(strategy.generate("SPY", february), expected)
        self.assertEqual(deepcopy(strategy).generate("SPY", february), expected)


if __name__ == "__main__":
    unittest.main()
