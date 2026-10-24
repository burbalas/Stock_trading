import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from trading_bot.evidence_watch import BUDGETS_EUR, SCENARIOS, ForwardTrial


FIRST = date(2026, 10, 23)
NOW = datetime(2028, 1, 1, 0, 0, tzinfo=timezone.utc)


def sessions(count):
    result = []
    day = FIRST
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def observations(days):
    return [{'date': day, 'budget_eur': budget, 'scenario': scenario,
             'candidate_equity': budget * 1.16 * (1 + index / 10000),
             'benchmark_equity': budget * 1.16 * (1 + index / 20000),
             'closed_round_trips': index // 2}
            for index, day in enumerate(days) for budget in BUDGETS_EUR for scenario in SCENARIOS]


class ForwardEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frozen = self.root / 'frozen.py'
        self.frozen.write_text('unchanged candidate rules\n', encoding='utf-8')
        self.archive = self.root / 'raw_and_actions.json'
        self.archive.write_text('{"actions":[]}', encoding='utf-8')
        self.trial = ForwardTrial.register(self.root / 'trial', trial_id='frozen-2026-10-22',
                                          frozen_files={'strategy': self.frozen}, configuration={'window': 189})
        self.metadata = {'actions_complete': True, 'costs_included': True,
                         'data_mode': 'raw_actions_verified', 'source_kind': 'archived_replay',
                         'real_execution': False, 'fees_verified': False,
                         'universe_point_in_time': False}

    def update(self, days, rows=None, **changes):
        parameters = {'observations': observations(days) if rows is None else rows,
                      'archived_files': {'raw_and_actions': self.archive},
                      'source_metadata': self.metadata, 'expected_sessions': days,
                      'completed_through': days[-1], 'now_utc': NOW}
        parameters.update(changes)
        return self.trial.update_prefix(**parameters)

    def test_new_trial_has_honest_zero_counts_and_no_market_calls(self):
        status = self.trial.status()
        self.assertEqual(status['status'], 'NO_FORWARD_DATA')
        self.assertEqual(status['first_eligible_date'], '2026-10-23')
        self.assertFalse(status['promotion_authorized'])
        self.assertEqual(len(status['cohorts']), 6)
        self.assertTrue(all(row['complete_market_sessions'] == 0 for row in status['cohorts']))
        self.assertFalse((self.trial.directory / 'HEAD.json').exists())
        report = self.trial.write_report().read_text(encoding='utf-8')
        self.assertIn('NO_FORWARD_DATA', report)
        self.assertIn('October21 trades do not qualify', report)

    def test_identical_registration_is_idempotent_but_changes_need_new_trial(self):
        again = ForwardTrial.register(self.trial.directory, trial_id='frozen-2026-10-22',
                                      frozen_files={'strategy': self.frozen}, configuration={'window': 189})
        self.assertEqual(self.trial.spec_sha256, again.spec_sha256)
        with self.assertRaisesRegex(RuntimeError, 'different rules'):
            ForwardTrial.register(self.trial.directory, trial_id='frozen-2026-10-22',
                                  frozen_files={'strategy': self.frozen}, configuration={'window': 190})

    def test_first_completed_prefix_produces_hashed_paired_csv_and_json(self):
        result = self.update(sessions(1))
        self.assertEqual(result['revision'], 1)
        self.assertTrue(all(row['complete_market_sessions'] == 1 for row in result['cohorts']))
        revision_files = list((self.trial.directory / 'revisions').iterdir())
        self.assertEqual(sorted(path.suffix for path in revision_files), ['.csv', '.json'])
        self.assertIn('candidate_equity,benchmark_equity', next(path for path in revision_files if path.suffix == '.csv').read_text())
        self.assertFalse(result['operational_minimum_counts_met'])

    def test_append_retains_original_immutable_snapshot_and_is_idempotent(self):
        self.update(sessions(1))
        original = {path: path.read_bytes() for path in (self.trial.directory / 'revisions').iterdir()}
        self.assertEqual(self.update(sessions(2))['revision'], 2)
        self.assertEqual(self.update(sessions(2))['revision'], 2)
        self.assertEqual(len(list((self.trial.directory / 'revisions').iterdir())), 4)
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)

    def test_changed_or_removed_old_observation_is_rejected(self):
        self.update(sessions(2))
        for change in ('value', 'remove'):
            rows = observations(sessions(3))
            if change == 'value':
                rows[0]['candidate_equity'] += 1
            else:
                del rows[0]
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'cannot be changed'):
                self.update(sessions(3), rows)
        self.assertEqual(self.trial.status()['revision'], 1)

    def test_legacy_and_plan_day_observations_do_not_qualify(self):
        for day in ['2026-10-21', '2026-10-22']:
            rows = observations(sessions(1))
            rows[0]['date'] = day
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, 'Backfilling'):
                self.update(sessions(1), rows)

    def test_daily_repetitions_do_not_inflate_session_counts(self):
        rows = observations(sessions(1))
        rows.append(dict(rows[0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate cohort'):
            self.update(sessions(1), rows)

    def test_gaps_are_reported_not_filled_and_cannot_be_retroactively_inserted(self):
        days = sessions(3)
        rows = [row for row in observations(days) if row['date'] != days[1]]
        result = self.update(days, rows)
        self.assertTrue(all(row['complete_market_sessions'] == 2 for row in result['cohorts']))
        self.assertTrue(all(row['missing_sessions'] == [days[1]] for row in result['cohorts']))
        with self.assertRaisesRegex(RuntimeError, 'cannot be changed'):
            self.update(sessions(4))

    def test_incomplete_or_future_session_is_rejected_even_if_input_claims_completion(self):
        with self.assertRaisesRegex(ValueError, 'incomplete or future'):
            self.update(sessions(1), now_utc=datetime(2026, 10, 23, 19, 59, tzinfo=timezone.utc))
        self.update(sessions(1), now_utc=datetime(2026, 10, 23, 20, 15, tzinfo=timezone.utc))

    def test_source_requirements_and_sizing_cannot_be_replaced_with_large_paper_account(self):
        for key, value in [('data_mode', 'all_adjusted'), ('actions_complete', False),
                           ('real_execution', True), ('costs_included', False)]:
            metadata = {**self.metadata, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'archived raw'):
                self.update(sessions(1), source_metadata=metadata)
        rows = observations(sessions(1))
        rows[0]['budget_eur'] = 100000
        with self.assertRaisesRegex(ValueError, 'representative cohorts'):
            self.update(sessions(1), rows)

    def test_source_code_changes_are_visible_and_block_updates(self):
        self.frozen.write_text('changed strategy\n', encoding='utf-8')
        self.assertEqual(self.trial.status()['frozen_artifact_changes'], ['strategy'])
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.update(sessions(1))

    def test_snapshot_and_csv_tampering_fail_integrity_checks(self):
        self.update(sessions(1))
        files = list((self.trial.directory / 'revisions').iterdir())
        for suffix in ['.csv', '.json']:
            path = next(path for path in files if path.suffix == suffix)
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(suffix=suffix), self.assertRaisesRegex(RuntimeError, 'integrity'):
                self.trial.status()
            path.write_bytes(original)

    def test_checkpoint_counts_are_never_live_authorization(self):
        result = self.update(sessions(60))
        self.assertTrue(result['operational_minimum_counts_met'])
        self.assertFalse(result['statistical_duration_checkpoint_met'])
        self.assertFalse(result['promotion_authorized'])
        result = self.update(sessions(253))
        self.assertTrue(result['statistical_duration_checkpoint_met'])
        self.assertFalse(result['promotion_authorized'])
        self.assertEqual(result['cohorts'][0]['paired_return_sessions'], 252)

    def test_invalid_equity_and_cumulative_trade_counts_rejected(self):
        for value in [0, float('nan'), float('inf'), -1, True]:
            rows = observations(sessions(1))
            rows[0]['candidate_equity'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.update(sessions(1), rows)
        rows = observations(sessions(5))
        rows[-1]['closed_round_trips'] = 0
        with self.assertRaisesRegex(ValueError, 'cannot decrease'):
            self.update(sessions(5), rows)


if __name__ == '__main__':
    unittest.main()
