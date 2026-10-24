"""Append-only progress for a frozen, representative-budget forward replay.

No network, credentials, brokerage adapter, process control, or automatic live
promotion is provided. An archive/replay producer supplies the complete prefix;
this module preserves it and rejects revisions to previously committed rows.
Its counts are simulation records, not independent broker execution evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .atomic_storage import cleanup_temporary, replace_file
from .journal import TradeJournal


SCENARIOS = ('base_5bps_fee0', 'stress_20bps_fee0', 'fee_sensitivity_20bps_fee1')
BUDGETS_EUR = (2500, 5000)
_ROW_FIELDS = ('date', 'budget_eur', 'scenario', 'candidate_equity', 'benchmark_equity',
               'closed_round_trips', 'candidate_invested', 'benchmark_invested')
_REQUIRED_ROW_FIELDS = set(_ROW_FIELDS[:6])
_HEX = re.compile(r'^[0-9a-f]{64}$')


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                           allow_nan=False) + '\n').encode('utf-8')
    except (ValueError, TypeError) as exc:
        raise ValueError('Trial metadata must contain only finite JSON values.') from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f'{field} must be a session date, not an intraday timestamp.')
    try:
        return value if isinstance(value, date) else date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{field} must be an ISO session date.') from exc


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{field} must be numeric, not boolean.')
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'{field} must be finite.') from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError(f'{field} must be finite and {"positive" if positive else "nonnegative"}.')
    return result


def _inventory(files: Mapping[str, str | Path]) -> dict[str, dict[str, str]]:
    if not files:
        raise ValueError('At least one archived or frozen source file is required.')
    result = {}
    for label, raw_path in sorted(files.items()):
        if not isinstance(label, str) or not label.strip():
            raise ValueError('Source file labels must be nonempty strings.')
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file():
            raise ValueError(f'Source is not a file: {label}')
        result[label] = {'path': str(path), 'sha256': _file_digest(path)}
    return result


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != content:
            raise RuntimeError(f'Conflicting immutable evidence file: {path.name}')


def _replace_json(path: Path, payload: Any) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
    finally:
        cleanup_temporary(temporary)


def _rows_csv(rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=_ROW_FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


class ForwardTrial:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        envelope = json.loads((self.directory / 'registration.json').read_text(encoding='utf-8'))
        self.spec = envelope['spec']
        self.spec_sha256 = _digest(_canonical(self.spec))
        if envelope.get('sha256') != self.spec_sha256 or self.spec.get('version') != 1:
            raise RuntimeError('Forward trial registration integrity check failed.')

    @classmethod
    def register(cls, directory: str | Path, *, trial_id: str,
                 frozen_files: Mapping[str, str | Path], configuration: Mapping[str, Any],
                 plan_date: str | date = '2026-10-22',
                 first_eligible_date: str | date = '2026-10-23',
                 budgets_eur: Sequence[int] = BUDGETS_EUR) -> 'ForwardTrial':
        if not isinstance(trial_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', trial_id):
            raise ValueError('trial_id must be a simple nonempty local identifier.')
        planned = _date(plan_date, 'plan_date')
        first = _date(first_eligible_date, 'first_eligible_date')
        if first <= planned or first.weekday() >= 5:
            raise ValueError('The first eligible session must be a weekday strictly after the plan date.')
        if tuple(budgets_eur) != BUDGETS_EUR or any(isinstance(value, bool) for value in budgets_eur):
            raise ValueError('This frozen trial requires EUR2500 and EUR5000, without budget selection.')
        if not isinstance(configuration, Mapping):
            raise ValueError('configuration must be a JSON mapping.')
        spec = {
            'version': 1, 'trial_id': trial_id, 'plan_date': planned.isoformat(),
            'first_eligible_date': first.isoformat(), 'budget_currency': 'EUR',
            'equity_currency': 'USD', 'budgets_eur': list(BUDGETS_EUR),
            'scenarios': list(SCENARIOS), 'benchmark': 'SPY_40pct_whole_share',
            'frozen_artifacts': _inventory(frozen_files), 'configuration': dict(configuration),
            'checkpoints': {'complete_market_sessions': 60, 'closed_round_trips': 20,
                            'statistical_return_sessions': 252},
        }
        content = _canonical({'spec': spec, 'sha256': _digest(_canonical(spec))})
        target = Path(directory).resolve()
        target.mkdir(parents=True, exist_ok=True)
        with TradeJournal(target).lock('forward-evidence', timeout=0):
            registration = target / 'registration.json'
            if registration.exists() and registration.read_bytes() != content:
                raise RuntimeError('A frozen trial already exists with different rules or artifacts; create a new trial.')
            _write_immutable(registration, content)
        return cls(target)

    def frozen_artifact_changes(self) -> list[str]:
        changed = []
        for label, artifact in self.spec['frozen_artifacts'].items():
            path = Path(artifact['path'])
            if not path.is_file() or _file_digest(path) != artifact['sha256']:
                changed.append(label)
        return changed

    def _snapshot(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        head_path = self.directory / 'HEAD.json'
        if not head_path.exists():
            return None, None
        head = json.loads(head_path.read_text(encoding='utf-8'))
        revision, digest = head.get('revision'), head.get('sha256')
        if type(revision) is not int or revision < 1 or not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise RuntimeError('Invalid forward evidence HEAD.')
        stem = f'{revision:06d}-{digest}'
        raw = (self.directory / 'revisions' / (stem + '.json')).read_bytes()
        if _digest(raw) != digest:
            raise RuntimeError('Forward evidence snapshot integrity check failed.')
        snapshot = json.loads(raw)
        if snapshot.get('spec_sha256') != self.spec_sha256 or snapshot.get('revision') != revision:
            raise RuntimeError('Forward evidence snapshot belongs to a different frozen trial.')
        csv_path = self.directory / 'revisions' / (stem + '.csv')
        if _file_digest(csv_path) != snapshot.get('csv_sha256'):
            raise RuntimeError('Forward evidence CSV integrity check failed.')
        return head, snapshot

    def update_prefix(self, observations: Sequence[Mapping[str, Any]], *,
                      archived_files: Mapping[str, str | Path], source_metadata: Mapping[str, Any],
                      expected_sessions: Sequence[str | date], completed_through: str | date,
                      now_utc: datetime | None = None) -> dict[str, Any]:
        """Commit a complete replay prefix; never fetch data or alter old rows.

        The producer must archive raw prices/actions and document modeled costs.
        Its provenance assertions are retained, not independently certified here.
        All six frozen cohorts must be replayed; omissions are visible gaps and
        do not count as complete sessions. Existing gaps cannot later be filled
        in place; corrected evidence requires a new, documented trial.
        """
        completed = _date(completed_through, 'completed_through')
        moment = now_utc or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError('now_utc must be timezone-aware.')
        local = moment.astimezone(ZoneInfo('America/New_York'))
        latest = local.date() if (local.hour, local.minute) >= (16, 15) else local.date() - timedelta(days=1)
        if completed > latest:
            raise ValueError('The prefix includes an incomplete or future regular-market session.')
        first = _date(self.spec['first_eligible_date'], 'first_eligible_date')
        if completed < first:
            raise ValueError('No eligible forward session has completed; keep the tracker empty.')
        sessions = [_date(value, 'expected_sessions') for value in expected_sessions]
        if not sessions or sessions != sorted(set(sessions)):
            raise ValueError('expected_sessions must be a nonempty strictly increasing unique calendar prefix.')
        if sessions[0] != first or sessions[-1] > completed or any(value.weekday() >= 5 for value in sessions):
            raise ValueError('Expected session calendar must begin at the first eligible session and end by completed_through.')
        if not isinstance(source_metadata, Mapping):
            raise ValueError('source_metadata must be a mapping.')
        required_assertions = {
            'actions_complete': True, 'costs_included': True,
            'data_mode': 'raw_actions_verified', 'source_kind': 'archived_replay',
            'real_execution': False,
        }
        if any(source_metadata.get(key) != value or
               (isinstance(value, bool) and type(source_metadata.get(key)) is not bool)
               for key, value in required_assertions.items()):
            raise ValueError('Forward replay requires archived raw/action-complete net-modeled-cost data, explicitly not real execution.')
        # Missing verification flags are recorded as unknown, never silently true.
        metadata = dict(source_metadata)
        metadata.setdefault('fees_verified', False)
        metadata.setdefault('universe_point_in_time', False)
        _canonical(metadata)
        rows: list[dict[str, Any]] = []
        seen = set()
        session_set = set(sessions)
        for source_row in observations:
            if not isinstance(source_row, Mapping) or not _REQUIRED_ROW_FIELDS <= set(source_row) or set(source_row) - set(_ROW_FIELDS):
                raise ValueError('Each observation must contain the required paired equity fields, with no unrecognized columns.')
            day = _date(source_row['date'], 'observation date')
            if day < first or day <= _date(self.spec['plan_date'], 'plan_date'):
                raise ValueError('Backfilling observations on or before the frozen plan is forbidden.')
            if day not in session_set:
                raise ValueError('Observation date is outside the supplied completed market calendar.')
            budget, scenario = source_row['budget_eur'], source_row['scenario']
            if isinstance(budget, bool) or budget not in BUDGETS_EUR or scenario not in SCENARIOS:
                raise ValueError('Observation budget/scenario is outside the six frozen representative cohorts.')
            closed = source_row['closed_round_trips']
            if type(closed) is not int or closed < 0:
                raise ValueError('closed_round_trips must be a nonnegative cumulative integer count.')
            key = (day.isoformat(), int(budget), scenario)
            if key in seen:
                raise ValueError('Duplicate cohort observation for a session is forbidden.')
            seen.add(key)
            row = {'date': day.isoformat(), 'budget_eur': int(budget), 'scenario': scenario,
                   'candidate_equity': _finite(source_row['candidate_equity'], 'candidate_equity', positive=True),
                   'benchmark_equity': _finite(source_row['benchmark_equity'], 'benchmark_equity', positive=True),
                   'closed_round_trips': closed}
            for field in _ROW_FIELDS[6:]:
                if field in source_row:
                    row[field] = _finite(source_row[field], field)
            rows.append(row)
        rows.sort(key=lambda row: (row['date'], row['budget_eur'], row['scenario']))
        previous_counts = {}
        for row in rows:
            cohort = (row['budget_eur'], row['scenario'])
            if row['closed_round_trips'] < previous_counts.get(cohort, 0):
                raise ValueError('Cumulative closed round trips cannot decrease within a cohort.')
            previous_counts[cohort] = row['closed_round_trips']
        sources = _inventory(archived_files)
        with TradeJournal(self.directory).lock('forward-evidence', timeout=0):
            if self.frozen_artifact_changes():
                raise RuntimeError('Frozen strategy/configuration/source code changed; create a new documented trial.')
            head, previous = self._snapshot()
            if previous:
                if completed < _date(previous['completed_through'], 'previous completed_through'):
                    raise ValueError('completed_through cannot move backwards.')
                old_rows = previous['observations']
                if rows[:len(old_rows)] != old_rows:
                    raise RuntimeError('Existing evidence rows cannot be changed, removed, inserted into, or reordered.')
                if sessions[:len(previous['expected_sessions'])] != [_date(day, 'previous session') for day in previous['expected_sessions']]:
                    raise RuntimeError('The previously committed expected calendar cannot be revised.')
                old_end = _date(previous['completed_through'], 'previous completed_through')
                if any(_date(row['date'], 'new date') <= old_end for row in rows[len(old_rows):]):
                    raise RuntimeError('A completed prefix cannot be backfilled; preserve the gap and document a new trial.')
                if (rows == old_rows and completed.isoformat() == previous['completed_through'] and
                        [day.isoformat() for day in sessions] == previous['expected_sessions'] and
                        sources == previous['archived_files'] and metadata == previous['source_metadata']):
                    return self.status()
            revision = 1 if head is None else head['revision'] + 1
            csv_content = _rows_csv(rows)
            snapshot = {
                'version': 1, 'revision': revision, 'spec_sha256': self.spec_sha256,
                'parent_sha256': None if head is None else head['sha256'],
                'completed_through': completed.isoformat(),
                'expected_sessions': [day.isoformat() for day in sessions],
                'observations': rows, 'archived_files': sources, 'source_metadata': metadata,
                'csv_sha256': _digest(csv_content),
            }
            content = _canonical(snapshot)
            digest = _digest(content)
            stem = f'{revision:06d}-{digest}'
            _write_immutable(self.directory / 'revisions' / (stem + '.json'), content)
            _write_immutable(self.directory / 'revisions' / (stem + '.csv'), csv_content)
            _replace_json(self.directory / 'HEAD.json', {'revision': revision, 'sha256': digest})
        return self.status()

    def status(self) -> dict[str, Any]:
        head, snapshot = self._snapshot()
        rows = [] if snapshot is None else snapshot['observations']
        expected = [] if snapshot is None else snapshot['expected_sessions']
        cohorts = []
        for budget in BUDGETS_EUR:
            for scenario in SCENARIOS:
                selected = [row for row in rows if row['budget_eur'] == budget and row['scenario'] == scenario]
                present = {row['date'] for row in selected}
                gaps = [session for session in expected if session not in present]
                closed = selected[-1]['closed_round_trips'] if selected else 0
                counts_met = len(selected) >= 60 and closed >= 20 and not gaps
                cohorts.append({
                    'budget_eur': budget, 'scenario': scenario,
                    'complete_market_sessions': len(selected), 'missing_sessions': gaps,
                    'paired_return_sessions': max(0, len(selected) - 1),
                    'closed_round_trips': closed, 'operational_minimum_counts_met': counts_met,
                    'statistical_duration_checkpoint_met': len(selected) - 1 >= 252 and not gaps,
                })
        changes = self.frozen_artifact_changes()
        return {
            'trial_id': self.spec['trial_id'], 'spec_sha256': self.spec_sha256,
            'first_eligible_date': self.spec['first_eligible_date'],
            'status': 'NO_FORWARD_DATA' if not rows else 'FORWARD_REPLAY_ACCUMULATING',
            'revision': 0 if head is None else head['revision'],
            'completed_through': None if snapshot is None else snapshot['completed_through'],
            'cohorts': cohorts, 'frozen_artifact_changes': changes,
            'operational_minimum_counts_met': bool(rows) and not changes and all(item['operational_minimum_counts_met'] for item in cohorts),
            'statistical_duration_checkpoint_met': bool(rows) and not changes and all(item['statistical_duration_checkpoint_met'] for item in cohorts),
            'promotion_authorized': False,
            'source_metadata': None if snapshot is None else snapshot['source_metadata'],
            'limitations': [
                'This tracker records an archived daily-bar replay, not verified representative-budget broker execution.',
                '60 sessions and 20 closed round trips are minimum review counts; 252 sessions is a descriptive statistical checkpoint. Neither proves profitability or authorizes funding.',
                'Source, action, fee, currency, no-external-cash-flow, and session-calendar assertions require review. This module does not independently certify them.',
                'Gaps are reported without padding. Existing roughly USD100000 account marks and October21 trades do not qualify as this prospective trial.',
            ],
        }

    def write_report(self, destination: str | Path | None = None) -> Path:
        report = self.status()
        target = Path(destination) if destination is not None else self.directory / 'progress.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [f'# Forward evidence: {report["trial_id"]}', '', f'Status: **{report["status"]}**', '',
                 f'First eligible completed market session: **{report["first_eligible_date"]}**.',
                 f'Frozen registration SHA-256: `{self.spec_sha256}`.', '',
                 '| Budget EUR | Scenario | Complete sessions | Closed round trips | Missing sessions |',
                 '|---:|---|---:|---:|---:|']
        for cohort in report['cohorts']:
            lines.append(f'| {cohort["budget_eur"]} | {cohort["scenario"]} | {cohort["complete_market_sessions"]} | {cohort["closed_round_trips"]} | {len(cohort["missing_sessions"])} |')
        lines += ['', 'No automatic promotion to real-money trading.', '']
        if report['frozen_artifact_changes']:
            lines += ['Frozen artifacts changed; updates are blocked: ' + ', '.join(report['frozen_artifact_changes']), '']
        lines.extend(report['limitations'])
        # A progress report is derived and replaceable; immutable input snapshots
        # remain in revisions/ with their paired CSV and SHA-256 manifest.
        descriptor, temporary = tempfile.mkstemp(prefix='progress-', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write('\n'.join(lines) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, target)
        finally:
            cleanup_temporary(temporary)
        return target


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('register', 'status', 'update', 'report'))
    parser.add_argument('--directory', required=True)
    parser.add_argument('--input', help='Local JSON registration or complete archive/replay prefix; never API credentials.')
    args = parser.parse_args(argv)
    payload = {} if not args.input else json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
    if args.action == 'register':
        trial = ForwardTrial.register(args.directory, **payload)
        result = trial.status()
    else:
        trial = ForwardTrial(args.directory)
        if args.action == 'update':
            result = trial.update_prefix(**payload)
        elif args.action == 'report':
            result = {'report': str(trial.write_report())}
        else:
            result = trial.status()
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
