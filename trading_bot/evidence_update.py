"""Collect read-only market data for the frozen prospective replay, then report.

No TradingClient, order submission, paper process control or funding action.
The large paper account is intentionally not mixed into the replay evidence.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .evidence_watch import ForwardTrial
from .profitability_evidence import PLAN_FILES, digest, run_study
from .research_archive import archive_research
from .time_utils import MARKET_TZ

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / 'var' / 'forward_evidence_2026-10-23'
FIRST = date(2026, 10, 23)


def frozen_files():
    names = ('profitability_evidence.py', 'evidence_update.py', 'research_archive.py',
             'research_accounting.py', 'research_benchmarks.py', 'evidence_statistics.py',
             'evidence_watch.py', 'strategies.py', 'advisor.py', 'portfolio.py', 'broker.py',
             'config.py', 'models.py', 'data.py', 'profitability_study.py', 'time_utils.py')
    return {**{name: ROOT / 'trading_bot' / name for name in names},
            **{name: ROOT / name for name in PLAN_FILES},
            'launcher': ROOT / 'Update-ProfitabilityEvidence.ps1'}


def registered_trial(directory=DIRECTORY, *, now=None):
    if (directory / 'registration.json').exists():
        return ForwardTrial(directory)
    moment = now or datetime.now(timezone.utc)
    if moment.astimezone(MARKET_TZ).date() >= FIRST:
        raise RuntimeError('Original prospective registration is missing. A new documented trial with a future start is required.')
    return ForwardTrial.register(directory, trial_id='relative-strength-189-2026-10-23',
        frozen_files=frozen_files(), configuration={
            'candidate': 'relative_strength_189', 'max_drawdown_pct': .15,
            'reference_usd_per_eur': 1.1234, 'reference_date': '2026-10-21',
            'raw_whole_share_execution': True, 'dividends': 'ex-date receivable; pay-date cash',
            'source_kind': 'archived_replay', 'real_execution': False,
            'representative_broker_execution_verified': False,
            'universe_point_in_time': False, 'fees_verified': False,
            'independent_exchange_calendar_verified': False,
            'expected_calendar': 'Observed SIP SPY session grid; common source omissions cannot be excluded.',
        })


def update_trial(*, directory=DIRECTORY, now=None):
    moment = now or datetime.now(timezone.utc)
    trial = registered_trial(directory, now=moment)
    if trial.frozen_artifact_changes():
        raise RuntimeError('Frozen trial files changed; review and document a new trial rather than revise old evidence.')
    # Conservative cutoff: previous New York calendar day, even after today's
    # close. Avoids delayed final bars and reusing a partially formed session.
    completed = moment.astimezone(MARKET_TZ).date() - timedelta(days=1)
    if completed < FIRST:
        trial.write_report()
        return trial.status()
    archive = directory / 'archives' / completed.isoformat()
    manifest = archive_research(archive, end=completed)
    if manifest.get('collection_succeeded') is not True:
        raise RuntimeError('Read-only market-data archive is incomplete; no forward evidence appended.')
    end = date.fromisoformat(manifest['bars']['raw']['per_symbol']['SPY']['last_date'])
    if end < FIRST:
        trial.write_report()
        return trial.status()
    study = directory / 'studies' / completed.isoformat()
    result_path = study / 'results.json'
    if not result_path.exists():
        run_study(archive, study, start=FIRST, end=end)
    results = json.loads(result_path.read_text(encoding='utf-8'))
    if results['archive_manifest_sha256'] != digest(archive / 'manifest.json'):
        raise RuntimeError('Study archive hash differs; existing evidence cannot be reused.')
    for name, expected in results['artifacts'].items():
        if Path(name).name != name or digest(study / name) != expected:
            raise RuntimeError('Study artifact integrity failed; existing evidence cannot be reused.')
    observations = json.loads((study / 'paired_observations.json').read_text(encoding='utf-8'))
    expected_sessions = sorted({row['date'] for row in observations})
    trial.update_prefix(observations,
        archived_files={'archive_manifest': archive / 'manifest.json', 'results': result_path,
                        **{name: archive / name for name in manifest['artifacts']}},
        source_metadata={'actions_complete': True, 'costs_included': True, 'data_mode': 'raw_actions_verified',
            'source_kind': 'archived_replay', 'real_execution': False, 'fees_verified': False,
            'universe_point_in_time': False, 'independent_exchange_calendar_verified': False,
            'coverage_caveat': 'Raw/split/all and supplied actions reconcile; provider revisions, small events and common calendar omissions remain possible.'},
        expected_sessions=expected_sessions, completed_through=end, now_utc=moment)
    trial.write_report()
    return trial.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('register', 'status', 'update'), nargs='?', default='update')
    args = parser.parse_args(argv)
    result = update_trial() if args.action == 'update' else registered_trial().status()
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
