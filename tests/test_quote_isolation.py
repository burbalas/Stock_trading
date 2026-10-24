from __future__ import annotations

from datetime import date
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Position, Side, Signal


DAY = date(2026, 10, 21)


def engine(directory, broker, quote, signals):
    symbols = list(signals)
    provider = SimpleNamespace(
        load=lambda requested: {symbol: [Candle(DAY, symbol, 100, 101, 99, 100, 1000)]
                                for symbol in requested},
        latest_entry_prices=Mock(side_effect=quote),
    )
    strategy = SimpleNamespace(generate=lambda symbol, history: Signal(symbol, signals[symbol], 1, "strategy"))
    advisor = SimpleNamespace(review=lambda signal, history: signal)
    return LiveTradingEngine(BotConfig(symbols=symbols, slippage_bps=0), provider, strategy,
                             advisor, broker, TradeJournal(directory))


class QuoteIsolationTests(unittest.TestCase):
    def test_one_bad_quote_preserves_valid_buy_in_preview_and_submit(self):
        for submit in (False, True):
            with self.subTest(submit=submit), TemporaryDirectory() as directory:
                broker = PaperBroker(10_000)
                broker.submit = Mock(wraps=broker.submit)

                def quote(symbols):
                    self.assertEqual(len(symbols), 1)
                    if symbols == ["BAD"]:
                        raise RuntimeError("Quote spread for BAD is 5.26%, above the 2% entry limit.")
                    return {"GOOD": 200}

                bot = engine(directory, broker, quote, {"BAD": Side.BUY, "GOOD": Side.BUY})
                decisions = {item.signal.symbol: item for item in bot.run_once(submit_orders=submit).decisions}
                self.assertIsNone(decisions["BAD"].order)
                self.assertIn("5.26%", decisions["BAD"].rejected_reason)
                self.assertEqual((decisions["GOOD"].order.quantity, decisions["GOOD"].order.price), (5, 200))
                self.assertEqual(broker.submit.call_count, int(submit))
                intents = bot.journal._read_state().get("order_intents", {})
                self.assertFalse(any(item["symbol"] == "BAD" for item in intents.values()))

    def test_all_bad_quotes_return_rejections_without_orders_or_cycle_failure(self):
        with TemporaryDirectory() as directory:
            broker = PaperBroker(10_000)
            broker.submit = Mock()
            bot = engine(directory, broker, RuntimeError("Stale or future quote, required 0-120s"),
                         {"A": Side.BUY, "B": Side.BUY})
            result = bot.run_once(submit_orders=True)
            self.assertEqual(len(result.decisions), 2)
            self.assertTrue(all(item.order is None for item in result.decisions))
            self.assertTrue(all("0-120s" in item.rejected_reason for item in result.decisions))
            broker.submit.assert_not_called()
            self.assertFalse(bot.journal._read_state().get("order_intents"))

    def test_missing_or_invalid_quote_does_not_fall_back_to_daily_close(self):
        for response in ({}, None, {"A": float("nan")}, {"A": 0}, {"A": True}):
            with self.subTest(response=response), TemporaryDirectory() as directory:
                broker = PaperBroker(10_000)
                broker.submit = Mock()
                bot = engine(directory, broker, lambda symbols: response, {"A": Side.BUY})
                decision = bot.run_once(submit_orders=True).decisions[0]
                self.assertIsNone(decision.order)
                self.assertIn("refusing stale-price sizing", decision.rejected_reason)
                broker.submit.assert_not_called()

    def test_failed_buy_quote_does_not_block_strategy_sell(self):
        with TemporaryDirectory() as directory:
            broker = PaperBroker(10_000)
            broker.positions["SELL"] = Position("SELL", 3, 100)
            broker.submit = Mock(wraps=broker.submit)
            bot = engine(directory, broker, RuntimeError("BUY quote is stale"),
                         {"BUY": Side.BUY, "SELL": Side.SELL})
            result = bot.run_once(submit_orders=True)
            decisions = {item.signal.symbol: item for item in result.decisions}
            self.assertIsNone(decisions["BUY"].order)
            self.assertEqual(decisions["SELL"].fill.quantity, 3)
            broker.submit.assert_called_once()
            self.assertEqual(broker.submit.call_args.args[0].side, Side.SELL)
            bot.data_provider.latest_entry_prices.assert_called_once_with(["BUY"])

    def test_failed_buy_quote_does_not_block_risk_exit(self):
        with TemporaryDirectory() as directory:
            broker = PaperBroker(10_000)
            broker.positions["STOP"] = Position("STOP", 3, 100)
            broker.position_prices = {"STOP": 80}
            broker.submit = Mock(wraps=broker.submit)
            bot = engine(directory, broker, RuntimeError("BUY quote is stale"),
                         {"BUY": Side.BUY, "STOP": Side.HOLD})
            result = bot.run_once(submit_orders=True)
            decisions = {item.signal.symbol: item for item in result.decisions}
            self.assertIn("stale", decisions["BUY"].rejected_reason)
            self.assertEqual(decisions["STOP"].order.reason, "stop loss")
            self.assertEqual(decisions["STOP"].fill.quantity, 3)
            broker.submit.assert_called_once()

    def test_fresh_quote_is_still_rechecked_before_valid_buy_submission(self):
        with TemporaryDirectory() as directory:
            broker = PaperBroker(10_000)
            broker.submit = Mock()
            responses = iter([RuntimeError("bad spread"), {"GOOD": 100}, RuntimeError("quote became stale")])

            def quote(symbols):
                response = next(responses)
                if isinstance(response, Exception):
                    raise response
                return response

            bot = engine(directory, broker, quote, {"BAD": Side.BUY, "GOOD": Side.BUY})
            decisions = {item.signal.symbol: item for item in bot.run_once(submit_orders=True).decisions}
            self.assertIn("bad spread", decisions["BAD"].rejected_reason)
            self.assertIn("execution revalidation", decisions["GOOD"].rejected_reason)
            self.assertIn("became stale", decisions["GOOD"].rejected_reason)
            self.assertIsNone(decisions["GOOD"].order)
            broker.submit.assert_not_called()
            self.assertFalse(bot.journal._read_state().get("order_intents"))


if __name__ == "__main__":
    unittest.main()
