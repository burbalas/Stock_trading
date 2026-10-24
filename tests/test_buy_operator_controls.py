from __future__ import annotations

from datetime import date
import json
from pathlib import Path
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


def engine_for(directory, symbols=("A",)):
    broker = PaperBroker(10_000)
    broker.refresh = Mock()
    broker.position_prices = {}
    broker.open_orders = Mock(return_value=[])
    broker.submit = Mock(wraps=broker.submit)
    broker.cancel_open_sell_orders = Mock()
    provider = SimpleNamespace(
        load=lambda requested: {
            symbol: [Candle(DAY, symbol, 100, 101, 99, 100, 1000)] for symbol in requested
        },
        latest_entry_prices=Mock(side_effect=lambda requested: {symbol: 100 for symbol in requested}),
    )
    strategy = SimpleNamespace(generate=lambda symbol, history: Signal(symbol, Side.BUY, 1, "entry"))
    advisor = SimpleNamespace(review=lambda signal, history: signal)
    engine = LiveTradingEngine(BotConfig(symbols=list(symbols)), provider, strategy, advisor,
                               broker, TradeJournal(directory))
    return engine, broker


class BuyOperatorControlTests(unittest.TestCase):
    def assert_entry_blocked(self, engine, broker, result, flag):
        decision = result.decisions[0]
        self.assertIsNone(decision.order)
        self.assertIsNone(decision.fill)
        self.assertIn("stop requested" if flag == "STOP" else "buys paused", decision.rejected_reason)
        self.assertFalse(engine.journal._read_state().get("order_intents"))
        broker.submit.assert_not_called()
        broker.cancel_open_sell_orders.assert_not_called()

    def test_control_requested_during_broker_refresh_prevents_entry(self):
        for flag in ("STOP", "STOP_BUYS"):
            with self.subTest(flag=flag), TemporaryDirectory() as directory:
                engine, broker = engine_for(directory)

                def refresh():
                    if broker.refresh.call_count == 2:
                        (engine.journal.directory / flag).touch()

                broker.refresh.side_effect = refresh
                self.assert_entry_blocked(engine, broker, engine.run_once(submit_orders=True), flag)

    def test_control_requested_during_fresh_quote_prevents_entry(self):
        for flag in ("STOP", "STOP_BUYS"):
            with self.subTest(flag=flag), TemporaryDirectory() as directory:
                engine, broker = engine_for(directory)

                def quote(requested):
                    if engine.data_provider.latest_entry_prices.call_count == 2:
                        (engine.journal.directory / flag).touch()
                    return {symbol: 100 for symbol in requested}

                engine.data_provider.latest_entry_prices.side_effect = quote
                self.assert_entry_blocked(engine, broker, engine.run_once(submit_orders=True), flag)

    def test_control_between_revalidation_and_reservation_is_visible_without_intent(self):
        for flag in ("STOP", "STOP_BUYS"):
            with self.subTest(flag=flag), TemporaryDirectory() as directory:
                engine, broker = engine_for(directory)
                revalidate = engine._revalidate_buy

                def request_after_revalidation(order, trade_date):
                    checked = revalidate(order, trade_date)
                    self.assertIsNotNone(checked[0])
                    (engine.journal.directory / flag).touch()
                    return checked

                engine._revalidate_buy = request_after_revalidation
                self.assert_entry_blocked(engine, broker, engine.run_once(submit_orders=True), flag)

    def test_first_completed_entry_does_not_allow_second_after_control(self):
        for flag in ("STOP", "STOP_BUYS"):
            with self.subTest(flag=flag), TemporaryDirectory() as directory:
                engine, broker = engine_for(directory, ("A", "B"))
                submit = broker.submit

                def fill_then_request(order, trade_date):
                    fill = submit(order, trade_date)
                    broker.position_prices[order.symbol] = order.price
                    (engine.journal.directory / flag).touch()
                    return fill

                broker.submit = Mock(side_effect=fill_then_request)
                decisions = engine.run_once(submit_orders=True).decisions
                broker.submit.assert_called_once()
                self.assertEqual(decisions[0].fill.symbol, "A")
                self.assertIsNone(decisions[1].order)
                self.assertIn("operator", decisions[1].rejected_reason)
                intents = engine.journal._read_state()["order_intents"]
                self.assertEqual([intent["symbol"] for intent in intents.values()], ["A"])

    def test_risk_exit_remains_available_with_operator_controls(self):
        for flag in ("STOP", "STOP_BUYS"):
            with self.subTest(flag=flag), TemporaryDirectory() as directory:
                engine, broker = engine_for(directory)
                broker.positions["A"] = Position("A", 5, 100)
                broker.position_prices["A"] = 80
                (engine.journal.directory / flag).touch()
                result = engine.run_risk_once(submit_orders=True)
                broker.submit.assert_called_once()
                self.assertEqual(result.decisions[0].fill.side, Side.SELL)
                self.assertEqual(result.decisions[0].fill.quantity, 5)
                self.assertEqual(broker.positions["A"].quantity, 0)

    def test_buy_controls_use_redirected_canonical_journal(self):
        with TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy"
            runtime = Path(directory) / "runtime"
            legacy.mkdir()
            runtime.mkdir()
            (legacy / ".runtime_redirect.json").write_text(
                json.dumps({"version": 1, "target": str(runtime)}), encoding="utf-8",
            )
            engine, broker = engine_for(legacy)
            (runtime / "STOP_BUYS").touch()
            self.assert_entry_blocked(engine, broker, engine.run_once(submit_orders=True), "STOP_BUYS")
            self.assertEqual(engine.journal.directory, runtime.resolve())


if __name__ == "__main__":
    unittest.main()
