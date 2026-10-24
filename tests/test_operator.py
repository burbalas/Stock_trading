import argparse
import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from trading_bot.cli import (
    _active_open_orders, _alpaca_protect, _live_config, _promotion_decision, _readiness_report,
    _run_alpaca_session, main,
)
from trading_bot.runtime import wait_for_next_check, write_heartbeat
from trading_bot.models import Order, Position, Side
from trading_bot.time_utils import MARKET_TZ


def session_args(directory, **overrides):
    values = dict(command="alpaca-session", cycles=2, until_close=False,
                  wait_for_open=True, submit=False, strategy="ma", journal_dir=directory,
                  risk_interval_minutes=5, signal_interval_minutes=390,
                  max_consecutive_errors=1, sell_only=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def market_clock(day=14, is_open=True):
    stamp = datetime(2026, 9, day, 10, tzinfo=MARKET_TZ)
    return SimpleNamespace(is_open=is_open, timestamp=stamp,
                           next_open=stamp + timedelta(days=1),
                           next_close=stamp.replace(hour=16))


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)

    def test_attempted_risk_exit_rechecks_protection_before_periodic_deadline(self):
        with TemporaryDirectory() as directory:
            args = session_args(directory, submit=True, protective_stop_sync_minutes=15)
            no_exit = SimpleNamespace(equity=100, decisions=[])
            attempted_exit = SimpleNamespace(equity=100, decisions=[SimpleNamespace(order=SimpleNamespace(side=Side.SELL))])
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker, \
                 patch("trading_bot.cli._run_alpaca_risk_once", side_effect=[no_exit, attempted_exit]), \
                 patch("trading_bot.cli._run_alpaca_once", return_value=no_exit), \
                 patch("trading_bot.cli._alpaca_protect") as protect, \
                 patch("trading_bot.cli._print_live_result"), \
                 patch("trading_bot.cli.wait_for_next_check", return_value=True):
                broker.return_value.market_clock.return_value = market_clock()
                _run_alpaca_session(args)
            self.assertEqual(protect.call_count, 2)

    def test_until_close_waits_for_first_open_then_exits_without_waiting_overnight(self):
        with TemporaryDirectory() as directory:
            args = session_args(directory, until_close=True)
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker, \
                 patch("trading_bot.cli._run_alpaca_risk_once", return_value=SimpleNamespace(equity=100)) as risk, \
                 patch("trading_bot.cli._run_alpaca_once", return_value=SimpleNamespace(equity=100)), \
                 patch("trading_bot.cli._print_live_result"), \
                 patch("trading_bot.cli.wait_for_next_check", return_value=True) as wait:
                broker.return_value.market_clock.side_effect = [market_clock(is_open=False), market_clock(), market_clock(is_open=False)]
                _run_alpaca_session(args)
            self.assertEqual(risk.call_count, 1)
            self.assertEqual(wait.call_count, 2)
            self.assertEqual(json.loads((Path(directory) / "heartbeat.json").read_text())["status"], "stopped")

    def test_new_market_date_resets_signal_deadline(self):
        with TemporaryDirectory() as directory:
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker, \
                 patch("trading_bot.cli._run_alpaca_risk_once", return_value=SimpleNamespace(equity=100)), \
                 patch("trading_bot.cli._run_alpaca_once", return_value=SimpleNamespace(equity=100)) as signal, \
                 patch("trading_bot.cli._print_live_result"), \
                 patch("trading_bot.cli.wait_for_next_check", return_value=True):
                broker.return_value.market_clock.side_effect = [market_clock(14), market_clock(15)]
                _run_alpaca_session(session_args(directory))
            self.assertEqual(signal.call_count, 2)

    def test_session_protects_entries_immediately_after_signal_submission(self):
        with TemporaryDirectory() as directory:
            entry = SimpleNamespace(equity=100, decisions=[SimpleNamespace(order=Order("AAPL", Side.BUY, 1, 100, "entry"))])
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker, \
                 patch("trading_bot.cli._run_alpaca_risk_once", return_value=SimpleNamespace(equity=100)), \
                 patch("trading_bot.cli._run_alpaca_once", return_value=entry), \
                 patch("trading_bot.cli._alpaca_protect") as protect, \
                 patch("trading_bot.cli._print_live_result"):
                broker.return_value.market_clock.return_value = market_clock()
                _run_alpaca_session(session_args(directory, cycles=1, submit=True, protective_stop_sync_minutes=15))
            self.assertEqual(protect.call_count, 2)

    def test_protection_resizes_remaining_position_without_lowering_stop(self):
        with TemporaryDirectory() as directory:
            args = argparse.Namespace(journal_dir=directory, submit=True, stop_loss_pct=.08, trailing_stop_pct=.05)
            order = SimpleNamespace(id="stop-1", client_order_id="bot-protect-aapl-2026-09-14", symbol="AAPL", side="sell", status="new", qty="10", stop_price="96", type="stop", filled_qty="0")
            position = SimpleNamespace(symbol="AAPL", qty="5", avg_entry_price="100", current_price="100", market_value="500")
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker, \
                 patch("trading_bot.cli._sync_alpaca_protective_fills"), \
                 patch("trading_bot.cli.reconcile_positions", return_value=SimpleNamespace(ok=True)):
                broker.return_value.positions = {"AAPL": Position("AAPL", 5, 100)}
                broker.return_value._client.get_all_positions.return_value = [position]
                broker.return_value.open_orders.return_value = [order]
                broker.return_value.replace_stop_order.return_value = SimpleNamespace(**{**vars(order), "qty": "5"})
                _alpaca_protect(args)
                broker.return_value.replace_stop_order.assert_called_once_with("stop-1", 96.0, 5)

    def test_protection_preserves_partially_filled_stop(self):
        with TemporaryDirectory() as directory:
            args = argparse.Namespace(journal_dir=directory, submit=True, stop_loss_pct=.08, trailing_stop_pct=.05)
            order = SimpleNamespace(id="stop-1", client_order_id="bot-protect-aapl-2026-09-14", symbol="AAPL", side="sell", status="partially_filled", qty="10", filled_qty="5", stop_price="96")
            position = SimpleNamespace(symbol="AAPL", qty="5", avg_entry_price="100", current_price="110", market_value="550")
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker, \
                 patch("trading_bot.cli._sync_alpaca_protective_fills"), \
                 patch("trading_bot.cli.reconcile_positions", return_value=SimpleNamespace(ok=True)):
                broker.return_value.positions = {"AAPL": Position("AAPL", 5, 100)}
                broker.return_value._client.get_all_positions.return_value = [position]
                broker.return_value.open_orders.return_value = [order]
                _alpaca_protect(args)
                broker.return_value.replace_stop_order.assert_not_called()
                broker.return_value.submit_protective_stop.assert_not_called()

    def test_stop_file_prevents_broker_access_and_interrupts_wait(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "STOP").touch()
            self.assertFalse(wait_for_next_check(86400, directory))
            with patch("trading_bot.cli.AlpacaPaperBroker") as broker:
                _run_alpaca_session(session_args(directory))
            broker.assert_not_called()

    def test_failed_session_has_observable_failure(self):
        with TemporaryDirectory() as directory:
            with patch("trading_bot.cli.AlpacaPaperBroker", side_effect=RuntimeError("connection unavailable")):
                with self.assertRaisesRegex(RuntimeError, "connection unavailable"):
                    _run_alpaca_session(session_args(directory))
            state = json.loads((Path(directory) / "heartbeat.json").read_text())
            self.assertEqual(state["status"], "failed")

    def test_pause_buys_file_preserves_sell_capability(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "STOP_BUYS").touch()
            values = dict(journal_dir=directory, cash=10000, short_window=10, long_window=30,
                          min_confidence=.55, max_open_positions=4, max_daily_orders=2,
                          max_position_pct=.25, max_order_pct=.1, target_cash_pct=.1,
                          stop_loss_pct=.08, slippage_bps=5, commission=0,
                          max_drawdown_stop_pct=.15, trailing_stop_pct=.05)
            config = _live_config(argparse.Namespace(**values), ["AAPL"])
            self.assertFalse(config.allow_new_buys)
            config.validate()

    def test_pending_cancel_and_replace_remain_active(self):
        orders = [SimpleNamespace(status=status) for status in ["pending_cancel", "pending_replace", "filled", "rejected"]]
        self.assertEqual(_active_open_orders(orders), orders[:2])

    def test_invalid_interval_rejected_before_broker_access(self):
        with patch("trading_bot.cli.AlpacaPaperBroker") as broker, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["alpaca-session", "--risk-interval-minutes", "nan"])
        self.assertEqual(raised.exception.code, 2)
        broker.assert_not_called()

    def test_invalid_standalone_protect_stops_rejected_before_broker_access(self):
        with patch("trading_bot.cli.AlpacaPaperBroker") as broker, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["alpaca-protect", "--stop-loss-pct", "nan", "--submit"])
        broker.assert_not_called()

    def test_incompatible_continuous_and_until_close_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["alpaca-session", "--continuous", "--until-close"])

    def test_heartbeat_is_valid_json_after_replacement(self):
        with TemporaryDirectory() as directory:
            write_heartbeat(directory, "running", equity=100)
            write_heartbeat(directory, "healthy", equity=110)
            data = json.loads((Path(directory) / "heartbeat.json").read_text())
            self.assertEqual(data["equity"], 110)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_readiness_cannot_accept_changed_results_with_old_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research.csv"
            walk = root / "walk.csv"
            research.write_text("strategy,test_return_pct\nfoo,999\n")
            walk.write_text("strategy,avg_edge_pct\nfoo,999\n")
            (root / "manifest.json").write_text(json.dumps({"schema_version": 1, "artifacts": {"research_sha256": "old"}}))
            report = _readiness_report(argparse.Namespace(research_csv=research, walk_forward_csv=walk,
                                       manifest=root / "manifest.json", output=root / "report.json"))
            self.assertEqual(report["status"], "insufficient_evidence")
            self.assertFalse(report["eligible_for_live"])
            self.assertIn("hashes do not match", report["blockers"][0])

    def test_promotion_selects_using_training_score(self):
        research = [dict(strategy="trained", train_score="10", test_return_pct="5", benchmark_return_pct="2"),
                    dict(strategy="lucky", train_score="1", test_return_pct="100", benchmark_return_pct="2")]
        walk = [dict(strategy=name, positive_folds="4", avg_edge_pct="2", worst_drawdown_pct="-5", avg_score=score)
                for name, score in [("trained", "5"), ("lucky", "100"), ("current", "1")]]
        report = _promotion_decision(research, walk, "current", 3, 0, 2, -15)
        self.assertEqual(report["candidate"], "trained")
        self.assertEqual(report["recommendation"], "PAPER CANDIDATE")

    def test_promotion_rejects_nonfinite_metric(self):
        rows = [dict(strategy="bad", positive_folds="nan", avg_score="100")]
        with self.assertRaisesRegex(RuntimeError, "invalid research metric"):
            _promotion_decision([], rows, "current", 3, 0, 2, -15)
