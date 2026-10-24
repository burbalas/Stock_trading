from datetime import date
from tempfile import TemporaryDirectory
import unittest

from trading_bot.broker import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.data import StaticMarketDataProvider
from trading_bot.journal import TradeJournal
from trading_bot.live import LiveTradingEngine
from trading_bot.models import Candle, Order, Position, Side, Signal


DAY = date(2026, 9, 14)


class BuyStrategy:
    def generate(self, symbol, history):
        return Signal(symbol, Side.BUY, .95, 'test entry')


class PassAdvisor:
    def review(self, signal, history):
        return signal


def engine(broker, journal):
    data = StaticMarketDataProvider({'A': [Candle(DAY, 'A', 10, 10, 10, 10, 1000)]})
    return LiveTradingEngine(BotConfig(symbols=['A'], starting_cash=1000, max_drawdown_stop_pct=.15),
                             data, BuyStrategy(), PassAdvisor(), broker, journal)


class DrawdownLatchTests(unittest.TestCase):
    def test_latch_survives_restart_rebound_and_threshold_change(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            self.assertEqual(journal.update_drawdown_state(1000, .15), (1000, 0, False))
            self.assertTrue(journal.update_drawdown_state(840, .15)[2])
            trigger = journal._read_state()['drawdown_halt']
            restarted = TradeJournal(directory)
            self.assertEqual(restarted.update_drawdown_state(1100, .50), (1100, 0, True))
            self.assertEqual(restarted._read_state()['drawdown_halt'], trigger)

    def test_live_signal_cycle_cannot_buy_after_recovery(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_peak_equity(1000)
            broker = PaperBroker(840)
            triggered = engine(broker, journal).run_once(submit_orders=True)
            self.assertIn('latched', triggered.decisions[0].rejected_reason)
            broker.cash = 1100
            recovered = engine(broker, TradeJournal(directory)).run_once(submit_orders=True)
            self.assertIn('latched', recovered.decisions[0].rejected_reason)
            self.assertFalse(broker.fills)

    def test_live_risk_cycle_keeps_liquidating_after_recovery(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_peak_equity(1000)
            broker = PaperBroker(740)
            broker.positions['A'] = Position('A', 10, 10)
            triggered = engine(broker, journal).run_risk_once()
            self.assertEqual(triggered.decisions[0].order.reason, 'account drawdown stop')
            broker.cash = 900  # Account recovers before the exit can execute.
            recovered = engine(broker, TradeJournal(directory)).run_risk_once(submit_orders=True)
            self.assertEqual(recovered.decisions[0].fill.side, Side.SELL)
            self.assertEqual(recovered.decisions[0].fill.reason, 'account drawdown stop')
            self.assertEqual(broker.positions['A'].quantity, 0)

    def test_atomic_reservation_blocks_buys_but_preserves_exits(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_drawdown_state(1000, .15)
            journal.update_drawdown_state(800, .15)
            self.assertFalse(journal.reserve_order(Order('A', Side.BUY, 1, 10, 'entry'), DAY))
            self.assertTrue(journal.reserve_order(Order('A', Side.SELL, 1, 10, 'exit'), DAY))

    def test_invalid_risk_state_cannot_reenable_buys(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal._write_state({'drawdown_halted': 'false'})
            with self.assertRaisesRegex(RuntimeError, 'latch is invalid'):
                journal.update_drawdown_state(1000, .15)
            with self.assertRaisesRegex(RuntimeError, 'latch is invalid'):
                journal.reserve_order(Order('A', Side.BUY, 1, 10, 'entry'), DAY)


if __name__ == '__main__':
    unittest.main()
