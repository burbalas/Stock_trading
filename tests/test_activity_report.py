from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_bot.activity_report import write_activity_report
from trading_bot.time_utils import LOCAL_TZ


NOW = datetime(2026, 10, 21, 12, tzinfo=LOCAL_TZ)


def observation(equity=1000, *, account="account-current-12345678", mode="paper-submit",
                timestamp="2026-10-20T15:00:00+00:00", market_date="2026-10-20", parameters=None):
    encoded = json.dumps(parameters or {"max_order_pct": .1}, sort_keys=True, separators=(",", ":"))
    return {"timestamp": timestamp, "market_date": market_date, "account_id": account,
            "mode": mode, "phase": "risk", "strategy": "relative-strength", "equity": equity,
            "drawdown_halted": False, "parameters_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "parameters_json": encoded}


def fill(**changes):
    row = {"timestamp": "2026-09-01T13:00:00+00:00", "mode": "alpaca-paper-sync",
           "date": "2026-09-01", "symbol": "A", "side": "buy", "quantity": 2,
           "price": 100, "value": 200, "reason": "legacy"}
    row.update(changes)
    return row


def write_csv(directory, name, rows):
    with (Path(directory) / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def state(directory, account="account-current-12345678"):
    (Path(directory) / "bot_state.json").write_text(json.dumps({"account_id": account}), encoding="utf-8")


def report(directory, broker=None, runtime=None):
    with patch("trading_bot.activity_report.now_local", return_value=NOW):
        path = write_activity_report(directory, broker or {}, runtime or {})
    return path, path.read_text(encoding="utf-8")


class ActivityReportTests(unittest.TestCase):
    def test_missing_ledgers_report_insufficient_evidence_without_profit(self):
        with TemporaryDirectory() as directory:
            path, text = report(directory)
            self.assertEqual(path, Path(directory) / "reports" / "activity-2026-10-21.md")
            self.assertIn("Evidence insufficient", text)
            self.assertIn("no confirmed account ID", text)
            self.assertIn("Recorded fills: **0**", text)
            self.assertIn("No profit or drawdown is inferred", text)
            self.assertNotIn("Maximum observed drawdown: 0.00%", text)
            self.assertFalse((Path(directory) / "bot_state.json").exists())
            self.assertFalse((Path(directory) / "fills.csv").exists())

    def test_previews_never_become_forward_paper_results(self):
        with TemporaryDirectory() as directory:
            state(directory)
            write_csv(directory, "account_observations.csv", [observation(10_000, mode="preview"),
                                                             observation(20_000, mode="preview")])
            _, text = report(directory)
            self.assertIn("Evidence insufficient", text)
            self.assertIn("Preview observations for the current account: 2", text)
            self.assertNotIn("First observed equity", text)
            self.assertNotIn("$20,000.00", text)

    def test_accounts_and_parameters_are_separate_and_observations_sorted(self):
        with TemporaryDirectory() as directory:
            state(directory)
            rows = [observation(900, timestamp="2026-10-21T10:00:00+00:00", market_date="2026-10-21"),
                    observation(1000),
                    observation(2000, parameters={"max_order_pct": .2}),
                    observation(2200, parameters={"max_order_pct": .2},
                                timestamp="2026-10-21T10:00:00+00:00", market_date="2026-10-21"),
                    observation(90_000, mode="preview"),
                    observation(70_000, account="another-account"),
                    observation(80_000, account="")]
            write_csv(directory, "account_observations.csv", rows)
            _, text = report(directory)
            self.assertIn("**4 observations** across **2 recorded market dates**, in **2 separate experiments**", text)
            self.assertIn("First observed equity: $1,000.00", text)
            self.assertIn("Latest observed equity: $900.00", text)
            self.assertIn("Maximum observed drawdown: 10.00%", text)
            self.assertIn("First observed equity: $2,000.00", text)
            self.assertIn("Latest observed equity: $2,200.00", text)
            self.assertIn("Other-account or unbound observations excluded: 2", text)
            self.assertIn("not cash-flow-adjusted returns", text)
            self.assertNotIn("$70,000.00", text)
            self.assertNotIn("$80,000.00", text)
            self.assertNotIn("$90,000.00", text)
            self.assertNotIn("account-current-12345678", text)
            self.assertIn("12345678", text)

    def test_unbound_journal_does_not_claim_known_observations(self):
        with TemporaryDirectory() as directory:
            write_csv(directory, "account_observations.csv", [observation(7000)])
            _, text = report(directory)
            self.assertIn("Evidence insufficient", text)
            self.assertIn("Other-account or unbound observations excluded: 1", text)
            self.assertNotIn("First observed equity", text)

    def test_one_observation_and_undated_rows_do_not_invent_sessions_or_drawdown(self):
        with TemporaryDirectory() as directory:
            state(directory)
            write_csv(directory, "account_observations.csv", [observation(market_date="")])
            _, text = report(directory)
            self.assertIn("**0 recorded market dates**", text)
            self.assertIn("without a recorded market date: 1", text)
            self.assertIn("Maximum observed drawdown: unavailable", text)

    def test_cached_broker_snapshot_is_separate_and_stale_activity_visible(self):
        with TemporaryDirectory() as directory:
            state(directory)
            broker = {"updated_at": "2026-10-01T10:00:00+00:00", "equity": 99440.16,
                      "cash": 99440.16, "positions": [], "open_orders": [], "reconciliation_ok": True}
            runtime = {"operator": {"state": "stale", "detail": "healthy",
                                    "updated_at": "2026-09-15T20:00:00+00:00"},
                       "supervisor": {"status": "running"}, "paused": True, "stop_requested": True}
            _, text = report(directory, broker, runtime)
            self.assertIn("Operator activity: **stale**", text)
            self.assertIn("Supervisor: running", text)
            self.assertIn("New buys are paused", text)
            self.assertIn("A stop has been requested", text)
            self.assertIn("2026-10-01T10:00:00+00:00 (stale)", text)
            self.assertIn("Cached equity: $99,440.16", text)
            self.assertIn("does not prove the bot is running", text)
            self.assertIn("Evidence insufficient", text)

    def test_legacy_fill_totals_are_execution_counts_without_current_candidate_profit(self):
        with TemporaryDirectory() as directory:
            state(directory)
            rows = [fill(), fill(side="sell", quantity=1, price=105, value=105),
                    fill(side="sell", quantity=1, price=106, value=106)]
            write_csv(directory, "fills.csv", rows)
            originals = {name: (Path(directory) / name).read_bytes() for name in ("fills.csv", "bot_state.json")}
            _, text = report(directory)
            self.assertIn("Recorded fills: **3**", text)
            self.assertIn("not completed round trips", text)
            self.assertIn("not attributed to the current candidate", text)
            self.assertIn("Buy fills: 1; execution value: $200.00", text)
            self.assertIn("Sell fills: 2; execution value: $211.00", text)
            self.assertIn("not profit", text)
            for name, original in originals.items():
                self.assertEqual((Path(directory) / name).read_bytes(), original)

    def test_nonfinite_or_invalid_observation_rows_fail_before_report_creation(self):
        for changes in ({"equity": "nan"}, {"equity": "inf"}, {"equity": -1},
                        {"mode": "backtest"}, {"timestamp": "2026-10-20T10:00:00"},
                        {"market_date": "bad-date"}, {"parameters_sha256": "a" * 64},
                        {"parameters_json": "{\"x\":NaN}"}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                row = observation()
                row.update(changes)
                write_csv(directory, "account_observations.csv", [row])
                with self.assertRaisesRegex(ValueError, "account_observations.csv row 2"):
                    report(directory)
                self.assertFalse((Path(directory) / "reports").exists())

    def test_nonfinite_or_inconsistent_fill_rows_fail_clearly(self):
        for changes in ({"quantity": "nan"}, {"price": "inf"}, {"value": "-Infinity"},
                        {"side": "hold"}, {"quantity": 0}, {"value": 900}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                write_csv(directory, "fills.csv", [fill(**changes)])
                with self.assertRaisesRegex(ValueError, "fills.csv row 2"):
                    report(directory)

    def test_malformed_required_csv_rows_fail_clearly(self):
        for content in ("equity\n1000\n", "timestamp,timestamp\na,b\n", ""):
            with self.subTest(content=content), TemporaryDirectory() as directory:
                (Path(directory) / "account_observations.csv").write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "account_observations.csv"):
                    report(directory)
        with TemporaryDirectory() as directory:
            write_csv(directory, "fills.csv", [fill()])
            with (Path(directory) / "fills.csv").open("a", encoding="utf-8") as handle:
                handle.write("incomplete,row\n")
            with self.assertRaisesRegex(ValueError, "fills.csv row 3"):
                report(directory)

    def test_bad_cached_numbers_do_not_generate_misleading_money(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Cached broker equity"):
                report(directory, {"equity": float("nan")})

    def test_failed_regeneration_preserves_existing_report_and_ledgers(self):
        with TemporaryDirectory() as directory:
            path, original = report(directory)
            write_csv(directory, "fills.csv", [fill(price="nan")])
            ledger = (Path(directory) / "fills.csv").read_bytes()
            with self.assertRaisesRegex(ValueError, "fills.csv"):
                report(directory)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual((Path(directory) / "fills.csv").read_bytes(), ledger)

    def test_finite_rows_cannot_produce_infinite_aggregate(self):
        with TemporaryDirectory() as directory:
            write_csv(directory, "fills.csv", [fill(quantity=1, price=1e308, value=1e308)] * 2)
            with self.assertRaisesRegex(ValueError, "total execution value"):
                report(directory)

    def test_mismatched_cached_account_is_visible(self):
        with TemporaryDirectory() as directory:
            state(directory)
            _, text = report(directory, {"account_suffix": "different", "equity": 500})
            self.assertIn("Cached broker account does not match the journal account", text)
            self.assertIn("cached values are not attributed", text)


if __name__ == "__main__":
    unittest.main()
