"""Operator activity and account observations, without placing broker orders."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from .journal import TradeJournal


def operator_status(journal_dir, *, now=None, stale_after_seconds=600):
    path = Path(journal_dir) / 'heartbeat.json'
    if not path.exists():
        return {'state': 'missing', 'detail': 'no operator heartbeat recorded'}
    try:
        heartbeat = json.loads(path.read_text(encoding='utf-8'))
        stamp = datetime.fromisoformat(heartbeat['updated_at'])
        status = heartbeat['status']
        if stamp.tzinfo is None or status not in {'running', 'healthy', 'waiting_for_open', 'stopped', 'failed'}:
            raise ValueError('invalid heartbeat timestamp/status')
        current = now or datetime.now(timezone.utc)
        age = (current - stamp).total_seconds()
        if age < -60:
            raise ValueError('heartbeat timestamp is in the future')
        state = status if status in {'stopped', 'failed'} else ('stale' if age > stale_after_seconds else 'recent')
        return {'state': state, 'detail': status, 'updated_at': stamp.isoformat(), 'age_seconds': max(0, age)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'invalid', 'detail': 'heartbeat unreadable; operator activity is unverified'}


def record_account_observation(journal_dir, equity, *, strategy, submit, phase, parameters, market_date=None):
    """Record observed equity, never infer returns without cash-flow evidence.

    Account ID, submit mode and full parameters distinguish evaluation runs.
    Previews remain visibly separate from submitting sessions. No brokerage
    request is made; equity comes from the completed risk or signal cycle.
    """
    if isinstance(equity, bool) or not isinstance(equity, (int, float)) or not math.isfinite(equity) or equity < 0:
        raise ValueError('Observed account equity must be finite and nonnegative.')
    if not isinstance(submit, bool) or phase not in {'risk', 'signal'}:
        raise ValueError('Invalid account observation mode or phase.')
    encoded_parameters = json.dumps(parameters, sort_keys=True, separators=(',', ':'), allow_nan=False)
    journal = TradeJournal(journal_dir)
    with journal.lock():
        state = journal._read_state()
        row = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'market_date': market_date.isoformat() if market_date else '',
            'account_id': state.get('account_id', ''),
            'mode': ('live-submit' if parameters.get('live_policy_path') else 'paper-submit') if submit else 'preview',
            'phase': phase, 'strategy': strategy, 'equity': repr(float(equity)),
            'drawdown_halted': state.get('drawdown_halted', False),
            'parameters_sha256': hashlib.sha256(encoded_parameters.encode('utf-8')).hexdigest(),
            'parameters_json': encoded_parameters,
        }
        journal._append_csv(journal.directory / 'account_observations.csv', list(row), row)
