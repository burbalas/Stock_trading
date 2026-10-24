import csv
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_bot.journal import TradeJournal
from trading_bot.monitoring import operator_status, record_account_observation


class MonitoringTests(unittest.TestCase):
    def test_old_running_heartbeat_is_stale_but_completed_session_remains_stopped(self):
        now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'heartbeat.json'
            for status, expected in [('running', 'stale'), ('healthy', 'stale'), ('stopped', 'stopped'), ('failed', 'failed')]:
                path.write_text(json.dumps({'updated_at': (now - timedelta(days=7)).isoformat(), 'status': status}))
                result = operator_status(directory, now=now)
                self.assertEqual(result['state'], expected)
                self.assertEqual(result['age_seconds'], 7 * 86400)
            path.write_text(json.dumps({'updated_at': (now - timedelta(seconds=30)).isoformat(), 'status': 'waiting_for_open'}))
            self.assertEqual(operator_status(directory, now=now)['state'], 'recent')

    def test_missing_corrupt_or_future_heartbeat_cannot_claim_recent_activity(self):
        now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            self.assertEqual(operator_status(directory, now=now)['state'], 'missing')
            path = Path(directory) / 'heartbeat.json'
            for payload in ('{', '{}', '[]', json.dumps({'status': 'healthy', 'updated_at': '2026-09-22T12:00:00'}),
                            json.dumps({'status': 'healthy', 'updated_at': (now + timedelta(hours=1)).isoformat()})):
                path.write_text(payload)
                self.assertEqual(operator_status(directory, now=now)['state'], 'invalid')

    def test_observations_distinguish_modes_accounts_and_parameter_changes(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.bind_account('paper-a')
            parameters = {'strategy': 'relative-strength', 'max_order_pct': .1}
            for submit, settings in [(False, parameters), (True, parameters), (True, dict(parameters, max_order_pct=.2))]:
                record_account_observation(directory, 1000, strategy='relative-strength', submit=submit,
                                           phase='risk', parameters=settings, market_date=date(2026, 9, 22))
            with (Path(directory) / 'account_observations.csv').open(newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row['mode'] for row in rows], ['preview', 'paper-submit', 'paper-submit'])
            self.assertTrue(all(row['account_id'] == 'paper-a' for row in rows))
            self.assertEqual(rows[0]['parameters_sha256'], rows[1]['parameters_sha256'])
            self.assertNotEqual(rows[1]['parameters_sha256'], rows[2]['parameters_sha256'])
            self.assertEqual(json.loads(rows[2]['parameters_json'])['max_order_pct'], .2)

    def test_invalid_equity_cannot_enter_observation_ledger(self):
        with TemporaryDirectory() as directory:
            for equity in (float('nan'), float('inf'), -1, True):
                with self.assertRaises(ValueError):
                    record_account_observation(directory, equity, strategy='test', submit=True,
                                               phase='risk', parameters={})
            self.assertFalse((Path(directory) / 'account_observations.csv').exists())


if __name__ == '__main__':
    unittest.main()
