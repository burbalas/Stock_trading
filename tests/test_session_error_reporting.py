from __future__ import annotations

import unittest
from argparse import Namespace
from unittest.mock import patch

from trading_bot.cli import _run_alpaca_session


class SessionErrorReportingTests(unittest.TestCase):
    def test_failure_heartbeat_oserror_preserves_original_session_exception(self) -> None:
        args = Namespace(journal_dir="unused-journal")
        original = RuntimeError("session operation failed")
        telemetry_error = PermissionError("heartbeat file locked")
        with patch("trading_bot.cli._run_alpaca_session_loop", side_effect=original), \
             patch("trading_bot.cli.write_heartbeat", side_effect=telemetry_error) as heartbeat:
            with self.assertRaises(RuntimeError) as raised:
                _run_alpaca_session(args)

        self.assertIs(raised.exception, original)
        self.assertEqual(original.__notes__, ["Could not write failure heartbeat: heartbeat file locked"])
        heartbeat.assert_called_once_with(args.journal_dir, "failed", error="session operation failed")

    def test_success_path_heartbeat_oserror_remains_visible(self) -> None:
        telemetry_error = PermissionError("heartbeat file locked")
        with patch("trading_bot.cli._run_alpaca_session_loop"), \
             patch("trading_bot.cli.write_heartbeat", side_effect=telemetry_error):
            with self.assertRaises(PermissionError) as raised:
                _run_alpaca_session(Namespace(journal_dir="unused-journal"))
        self.assertIs(raised.exception, telemetry_error)

    def test_failure_heartbeat_programming_error_is_not_swallowed(self) -> None:
        original = RuntimeError("session operation failed")
        telemetry_error = ValueError("invalid heartbeat payload")
        with patch("trading_bot.cli._run_alpaca_session_loop", side_effect=original), \
             patch("trading_bot.cli.write_heartbeat", side_effect=telemetry_error):
            with self.assertRaises(ValueError) as raised:
                _run_alpaca_session(Namespace(journal_dir="unused-journal"))
        self.assertIs(raised.exception, telemetry_error)
        self.assertIs(telemetry_error.__context__, original)


if __name__ == "__main__":
    unittest.main()
