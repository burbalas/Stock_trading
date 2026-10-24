"""Desktop-facing controls and a supervised local Alpaca paper operator."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .atomic_storage import cleanup_temporary, replace_file
from .journal import TradeJournal
from .monitoring import operator_status
from .paper_control import paper_execution_lock
from .runtime_paths import canonical_runtime_directory, default_paper_directory, prepare_paper_runtime, resolve_journal_directory


PROJECT = Path(__file__).resolve().parents[1]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except (TypeError, ValueError):
        return None


def _network_error(error):
    """Retry transport outages only; account/data/authentication errors need review."""
    from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout
    if isinstance(error, (RequestsConnectionError, Timeout, ConnectionError, TimeoutError)):
        return True
    message = str(error).lower()
    return any(fragment in message for fragment in (
        'connectionerror', 'connecttimeout', 'readtimeout', 'connection timed out',
        'read timed out', 'name resolution', 'getaddrinfo failed', 'max retries exceeded',
        'remote disconnected', 'remotedisconnected', 'connectionreseterror',
        'network is unreachable', 'temporary failure in name',
    ))


def _windows():
    return os.name == 'nt'


def _set_thread_execution_state(flags):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = kernel.SetThreadExecutionState
    function.argtypes = [wintypes.DWORD]
    function.restype = wintypes.DWORD
    return function(flags)


@contextmanager
def _keep_system_awake():
    """Prevent idle sleep for this manager thread; never change a power plan."""
    status = {'sleep_inhibited': False, 'power_warning': None}
    if _windows():
        try:
            status['sleep_inhibited'] = bool(_set_thread_execution_state(0x80000001))
        except (OSError, AttributeError):
            pass
        if not status['sleep_inhibited']:
            status['power_warning'] = 'Windows sleep prevention failed. Keep this computer awake manually while monitoring is enabled.'
    try:
        yield status
    finally:
        if status['sleep_inhibited']:
            try:
                _set_thread_execution_state(0x80000000)
            except (OSError, AttributeError):
                pass


def _read(path):
    if not path.exists():
        return {}
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise RuntimeError(f'Invalid application state: {path.name}')
    return result


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
    finally:
        cleanup_temporary(temporary)


def candidate_arguments(project, directory):
    """Preserve the frozen candidate and capital budget across launch paths."""
    return [
        'alpaca-session', '--symbols-file', str(project / 'universe_popular.txt'),
        '--profile', 'custom', '--strategy', 'relative-strength', '--cash', '100000',
        '--days', '600', '--feed', 'iex', '--short-window', '20', '--long-window', '100',
        '--min-confidence', '0.68', '--max-open-positions', '4', '--max-daily-orders', '2',
        '--max-position-pct', '0.25', '--max-order-pct', '0.10', '--target-cash-pct', '0.08',
        '--stop-loss-pct', '0.10', '--trailing-stop-pct', '0.10',
        '--max-drawdown-stop-pct', '0.15', '--slippage-bps', '5', '--commission', '0',
        '--entry-limit-bps', '5',
        '--buy-cooldown-days', '3', '--journal-dir', str(directory), '--submit', '--continuous',
        '--risk-interval-minutes', '5', '--signal-interval-minutes', '390',
        '--open-buffer-minutes', '10', '--close-buffer-minutes', '15',
    ]


def protected_symbols(positions, orders):
    """Confirm coverage from active stop orders, not from the local intention."""
    protected = []
    for position in positions:
        for order in orders:
            if (order['symbol'] == position['symbol'] and order['side'] == 'sell'
                    and order.get('type') == 'stop' and order.get('managed')
                    and order['status'] in {'new', 'accepted', 'accepted_for_bidding', 'partially_filled'}
                    and (order.get('stop_price') or 0) > 0
                    and order['quantity'] - order.get('filled_quantity', 0) >= position['quantity']):
                protected.append(position['symbol'])
                break
    return protected


class PaperApplication:
    def __init__(self, project_dir=None, runtime_dir=None):
        self.project = Path(project_dir or PROJECT).resolve()
        self.directory = canonical_runtime_directory(runtime_dir or default_paper_directory())
        self._supervising = False
        self._power_status = None

    def ensure_runtime(self):
        if ((self.directory / '.runtime_migration.json').exists()
                and resolve_journal_directory(self.project / 'var') == self.directory):
            return self.directory
        with paper_execution_lock():
            self.directory = prepare_paper_runtime(self.project / 'var', self.directory)
        return self.directory

    def _manager_alive(self):
        if self._supervising:
            return True
        if not self.directory.exists():
            return False
        try:
            with TradeJournal(self.directory).lock('manager', timeout=0):
                return False
        except RuntimeError:
            return True

    def _worker_alive(self):
        if not self.directory.exists():
            return False
        try:
            with TradeJournal(self.directory).lock('execution', timeout=0):
                return False
        except RuntimeError:
            return True

    def status(self):
        from .startup import startup_enabled
        directory = resolve_journal_directory(self.directory)
        supervisor = _read(directory / 'supervisor.json')
        alive = self._manager_alive()
        worker_alive = self._worker_alive()
        control = _read(directory / 'control.json')
        if supervisor.get('status') in {'running', 'restarting', 'starting', 'stopping', 'waiting_for_network'} and not alive and not self._starting(supervisor):
            supervisor = dict(supervisor, status='stopped', last_error=supervisor.get('last_error') or 'Background manager is not running.')
        if not alive and worker_alive:
            supervisor = dict(supervisor, status='stopping' if (directory / 'STOP').exists() else 'unmanaged',
                              last_error='A worker is still active without its manager. Stop and wait before restarting.')
        supervisor['manager_running'] = alive
        supervisor['worker_running'] = worker_alive
        supervisor['sleep_inhibited'] = bool(alive and supervisor.get('sleep_inhibited'))
        supervisor['desired_enabled'] = bool(control.get('enabled'))
        supervisor['recovery_required'] = bool(control.get('enabled') and not alive and not worker_alive
                                                and not self._starting(supervisor) and not (directory / 'STOP').exists())
        if supervisor['recovery_required']:
            supervisor['last_error'] = ('Trading was left enabled, but its manager is absent. Start paper trading to recover. '
                                        'Windows sign-in resume is available as an optional setting.')
        manager_age = _age_seconds(supervisor.get('updated_at'))
        supervisor['manager_age_seconds'] = manager_age
        supervisor['manager_stale'] = bool(alive and (manager_age is None or manager_age < -60 or manager_age > 120))
        activity = operator_status(directory)
        supervisor['attention_required'] = (supervisor['recovery_required'] or supervisor['manager_stale']
                                           or (worker_alive and activity['state'] in {'stale', 'invalid', 'failed'}))
        if supervisor['manager_stale']:
            supervisor['last_error'] = 'Manager process exists, but its status is stale. Monitoring activity is unverified; review the log before restarting.'
        elif worker_alive and activity['state'] in {'stale', 'invalid', 'failed'}:
            supervisor['last_error'] = 'Worker process exists, but its heartbeat is not healthy. Review the log; do not launch a second worker.'
        return {
            'runtime_dir': str(directory), 'supervisor': supervisor,
            'operator': activity, 'broker': _read(directory / 'broker_status.json'),
            'paused': (directory / 'STOP_BUYS').exists(), 'stop_requested': (directory / 'STOP').exists(),
            'autostart': startup_enabled(),
            'log_path': str(self._log_path() or ''),
        }

    def refresh_broker(self):
        from .alpaca_integration import AlpacaPaperBroker
        from .reconcile import reconcile_positions, sync_broker_orders

        self.ensure_runtime_if_needed()
        journal = TradeJournal(self.directory)
        broker = AlpacaPaperBroker()
        state = journal._read_state()
        expected = _read(self.directory / 'control.json').get('expected_account_id') or state.get('account_id')
        if expected and expected != broker.account_id:
            raise RuntimeError('Connected account differs from the saved paper account. No state was reset.')
        sync_broker_orders(journal, broker)
        reconciliation = reconcile_positions(journal, broker)
        clock = broker.market_clock()
        snapshot = broker.snapshot(broker.position_prices)
        orders = broker.open_orders()
        protective_ids = {value.get('client_order_id') for value in journal._read_state().get('protective_orders', {}).values()}
        result = {
            'updated_at': _now(), 'account_suffix': (broker.account_id or '')[-6:],
            'equity': snapshot.equity, 'cash': snapshot.cash,
            'positions': [{'symbol': p.symbol, 'quantity': p.quantity, 'average_cost': p.average_cost,
                           'current_price': broker.position_prices.get(p.symbol)} for p in broker.positions.values()],
            'open_orders': [{'symbol': str(o.symbol), 'side': str(getattr(o.side, 'value', o.side)),
                             'quantity': float(o.qty), 'status': str(getattr(o.status, 'value', o.status)),
                             'type': str(getattr(o.type, 'value', o.type)), 'stop_price': float(o.stop_price) if o.stop_price else None,
                             'filled_quantity': float(o.filled_qty or 0),
                             'managed': str(o.client_order_id).startswith('bot-protect-') or str(o.client_order_id) in protective_ids} for o in orders],
            'reconciliation_ok': reconciliation.ok, 'reconciliation_messages': reconciliation.messages,
            'market_open': bool(clock.is_open), 'next_open': clock.next_open.isoformat(), 'next_close': clock.next_close.isoformat(),
        }
        result['protected_symbols'] = protected_symbols(result['positions'], result['open_orders'])
        result['unprotected_symbols'] = [p['symbol'] for p in result['positions'] if p['symbol'] not in result['protected_symbols']]
        _write(self.directory / 'broker_status.json', result)
        return result

    def ensure_runtime_if_needed(self):
        if not (self.directory / '.runtime_migration.json').exists():
            self.ensure_runtime()

    def start(self):
        self.ensure_runtime_if_needed()
        if self._manager_alive() or self._starting(_read(self.directory / 'supervisor.json')):
            return self.status()
        with TradeJournal(self.directory).lock('control'), paper_execution_lock():
            if self._manager_alive():
                return self.status()
            snapshot = self.refresh_broker()
            if not snapshot['reconciliation_ok']:
                raise RuntimeError('Account reconciliation failed. Review the account before starting.')
            expected = TradeJournal(self.directory)._read_state().get('account_id')
            if not expected:
                raise RuntimeError('Paper account identity could not be confirmed.')
            # Only an explicit Start clears a prior full stop; buy pause/latch stay.
            (self.directory / 'STOP').unlink(missing_ok=True)
            _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': expected, 'updated_at': _now()})
            try:
                self._launch_background()
            except Exception:
                _write(self.directory / 'control.json', {'enabled': False, 'expected_account_id': expected, 'updated_at': _now()})
                self._state('failed', last_error='Could not launch the background manager.')
                raise
        return self.status()

    def _launch_background(self):
        logs = self.directory / 'logs'
        logs.mkdir(exist_ok=True)
        log = logs / f'manager-{time.time_ns()}.log'
        self._state('starting', log_path=str(log), restarts=0)
        environment = os.environ.copy()
        environment.update({'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        command = [self._python(), '-u', '-m', 'trading_bot.paper_app', 'supervise',
                   '--project-dir', str(self.project), '--runtime-dir', str(self.directory),
                   '--manager-log', str(log)]
        self._launch_manager(command, log, environment)

    def _launch_manager(self, command, log, environment):
        if os.name == 'nt':
            # Shell launch survives terminal/job cleanup, including the Windows
            # Store Python redirector. The manager opens its own log after launch.
            def literal(value):
                return "'" + str(value).replace("'", "''") + "'"

            executable = Path(command[0]).with_name('pythonw.exe')
            if not executable.exists():
                executable = Path(command[0])
            script = (
                "$ErrorActionPreference = 'Stop'; Start-Process -WindowStyle Hidden -FilePath " + literal(executable)
                + ' -ArgumentList ' + literal(subprocess.list2cmdline(command[1:]))
                + ' -WorkingDirectory ' + literal(self.project)
            )
            subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                           cwd=self.project, env=environment, capture_output=True, text=True,
                           check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            with log.open('w', encoding='utf-8') as output:
                subprocess.Popen(command, cwd=self.project, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

    @staticmethod
    def _starting(state):
        if state.get('status') != 'starting':
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state['updated_at'])).total_seconds()
            return 0 <= age < 30
        except (KeyError, ValueError, TypeError):
            return False

    def stop(self):
        self.ensure_runtime_if_needed()
        with TradeJournal(self.directory).lock('control'):
            control = _read(self.directory / 'control.json')
            _write(self.directory / 'control.json', dict(control, enabled=False, updated_at=_now()))
            (self.directory / 'STOP').touch()
            current = _read(self.directory / 'supervisor.json')
            self._state('stopping' if self._manager_alive() or self._worker_alive() else 'stopped', **{
                k: v for k, v in current.items() if k not in {'status', 'updated_at'}
            })
        return self.status()

    def pause_buys(self):
        self.ensure_runtime_if_needed()
        (self.directory / 'STOP_BUYS').touch()

    def resume_buys(self):
        self.ensure_runtime_if_needed()
        if TradeJournal(self.directory)._read_state().get('drawdown_halted'):
            raise RuntimeError('The account drawdown halt is latched; removing a buy pause cannot clear it.')
        (self.directory / 'STOP_BUYS').unlink(missing_ok=True)

    def set_autostart(self, enabled):
        from .startup import set_startup
        self.ensure_runtime_if_needed()
        set_startup(enabled, self.project, self.directory)

    def resume(self):
        # Sign-in often precedes network availability. Launch only the manager;
        # it preflights the broker with transport recovery before any worker.
        # Never clear STOP or change the persisted desired state here.
        if (not _read(self.directory / 'control.json').get('enabled')
                or (self.directory / 'STOP').exists()):
            return self.status()
        self.ensure_runtime_if_needed()
        if self._manager_alive() or self._starting(_read(self.directory / 'supervisor.json')):
            return self.status()
        with TradeJournal(self.directory).lock('control'), paper_execution_lock():
            control = _read(self.directory / 'control.json')
            if control.get('enabled') and not (self.directory / 'STOP').exists() and not self._manager_alive():
                if not control.get('expected_account_id') or TradeJournal(self.directory)._read_state().get('account_id') != control['expected_account_id']:
                    raise RuntimeError('Saved paper account identity changed; explicitly Start after review.')
                self._launch_background()
        return self.status()

    def _log_path(self):
        path = _read(self.directory / 'supervisor.json').get('log_path')
        if path:
            candidate = Path(path).resolve()
            if candidate.is_relative_to((self.directory / 'logs').resolve()):
                return candidate
        return None

    def logs(self):
        path = self._log_path()
        if not path or not path.exists():
            return 'No managed session log yet.'
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 48000))
            text = handle.read().decode('utf-8', errors='replace')
        error_path = path.with_suffix('.error.log')
        if error_path.exists():
            with error_path.open('rb') as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 12000))
                text += '\n' + handle.read().decode('utf-8', errors='replace')
        return text

    def write_report(self):
        from .activity_report import write_activity_report
        return write_activity_report(self.directory, _read(self.directory / 'broker_status.json'), self.status())

    def _state(self, status, **details):
        if self._power_status is not None:
            details.update(self._power_status)
        _write(self.directory / 'supervisor.json', {'updated_at': _now(), 'status': status, **details})

    def _python(self):
        executable = self.project / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        return str(executable if executable.exists() else Path(sys.executable))

    def supervise(self):
        """Bounded crash recovery; a user stop always wins over recovery."""
        with TradeJournal(self.directory).lock('manager', timeout=0):
            if not self._enabled():
                self._state('stopped', reason='Stopped by user or STOP flag.', log_path=str(self._log_path() or ''))
                return
            self._supervising = True
            try:
                with _keep_system_awake() as power_status:
                    self._power_status = power_status
                    self._supervise()
            finally:
                self._supervising = False
                self._power_status = None

    def _enabled(self):
        return bool(_read(self.directory / 'control.json').get('enabled') and not (self.directory / 'STOP').exists())

    def _pulse(self):
        state = _read(self.directory / 'supervisor.json')
        self._state(state.pop('status', 'running'), **{k: v for k, v in state.items() if k != 'updated_at'})

    def _wait_for_retry(self, seconds):
        """Keep Stop responsive during a network outage or worker backoff."""
        for second in range(seconds):
            if not self._enabled():
                return False
            if second % 15 == 0:
                self._pulse()
            time.sleep(1)
        return self._enabled()

    def _network_recovery(self, error, count, log=None):
        delay = min(300, 5 * (2 ** min(count - 1, 6)))
        details = {'last_error': str(error), 'network_retries': count, 'retry_in_seconds': delay}
        if log:
            details['log_path'] = str(log)
        else:
            details['log_path'] = str(self._log_path() or '')
        self._state('waiting_for_network', **details)
        return self._wait_for_retry(delay)

    def _supervise(self):
        attempts = []
        network_failures = 0
        last_report = None
        while self._enabled():
            control = _read(self.directory / 'control.json')
            expected = control.get('expected_account_id')
            if not expected or TradeJournal(self.directory)._read_state().get('account_id') != expected:
                self._fail('Managed paper account identity changed; review before restarting.')
                return
            try:
                snapshot = self.refresh_broker()
                if not snapshot['reconciliation_ok']:
                    raise RuntimeError('Account reconciliation failed; review before restarting.')
            except Exception as exc:
                if not _network_error(exc):
                    self._fail(str(exc))
                    return
                network_failures += 1
                if not self._network_recovery(exc, network_failures):
                    break
                continue
            if not self._enabled():
                break
            attempts = [t for t in attempts if time.monotonic() - t < 900]
            if len(attempts) >= 3:
                self._fail('Three starts failed within 15 minutes. Review the log and explicitly Start to retry.')
                return
            attempts.append(time.monotonic())
            log = self.directory / 'logs' / f'paper-{time.time_ns()}.log'
            log.parent.mkdir(exist_ok=True)
            environment = os.environ.copy()
            environment.update({'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
                                'STOCK_TRADING_EXPECTED_ACCOUNT_ID': expected})
            command = [self._python(), '-u', '-m', 'trading_bot.cli', *candidate_arguments(self.project, self.directory)]
            launched_at = datetime.now(timezone.utc)
            launched_monotonic = time.monotonic()
            with log.open('w', encoding='utf-8') as output:
                options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                process = subprocess.Popen(command, cwd=self.project, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, **options)
                self._state('running', worker_pid=process.pid, log_path=str(log), restarts=len(attempts) - 1)
                last_pulse = time.monotonic()
                while process.poll() is None:
                    if not _read(self.directory / 'control.json').get('enabled'):
                        (self.directory / 'STOP').touch()
                    if time.monotonic() - last_pulse >= 15:
                        self._pulse()
                        last_pulse = time.monotonic()
                    heartbeat = _read(self.directory / 'heartbeat.json')
                    if heartbeat.get('status') == 'waiting_for_open':
                        today = datetime.now(timezone.utc).date().isoformat()
                        if last_report != today:
                            try:
                                self.write_report()
                            except (OSError, ValueError, RuntimeError) as exc:
                                # A report is ancillary: preserve risk monitoring if
                                # historical CSV data or a report destination is bad.
                                self._state('running', worker_pid=process.pid, log_path=str(log),
                                            restarts=len(attempts) - 1, report_error=str(exc))
                            last_report = today
                    time.sleep(1)
            if not self._enabled():
                break
            heartbeat = _read(self.directory / 'heartbeat.json')
            try:
                heartbeat_is_current = datetime.fromisoformat(heartbeat['updated_at']) >= launched_at
            except (KeyError, ValueError, TypeError):
                heartbeat_is_current = False
            error = ((heartbeat.get('error') if heartbeat_is_current else None)
                     or self.logs()[-3000:] or f'Worker exited with code {process.returncode}.')
            # Data/account inconsistencies need review, never an automatic retry.
            if any(word in error.lower() for word in ('reconcil', 'differs', 'ambiguous', 'fractional', 'whole-share', 'unsupported')):
                self._fail(error, log_path=str(log))
                return
            if _network_error(error):
                attempts.pop()  # A transport outage does not exhaust crash recovery.
                if time.monotonic() - launched_monotonic >= 300:
                    network_failures = 0
                network_failures += 1
                if not self._network_recovery(error, network_failures, log):
                    break
                continue
            self._state('restarting', last_error=error, log_path=str(log), restarts=len(attempts))
            if not self._wait_for_retry(5 * len(attempts)):
                break
        self._state('stopped', reason='Stopped by user or STOP flag.', log_path=str(self._log_path() or ''))

    def _fail(self, message, **details):
        details.setdefault('log_path', str(self._log_path() or ''))
        (self.directory / 'STOP').touch()
        control = _read(self.directory / 'control.json')
        _write(self.directory / 'control.json', dict(control, enabled=False, updated_at=_now()))
        self._state('failed', last_error=message, **details)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage the local paper trading application.')
    parser.add_argument('action', choices=['supervise', 'status', 'start', 'stop', 'resume', 'pause-buys', 'resume-buys', 'report'])
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--project-dir', type=Path)
    parser.add_argument('--manager-log', type=Path)
    args = parser.parse_args(argv)
    app = PaperApplication(args.project_dir, args.runtime_dir)
    if args.action == 'supervise':
        def supervise():
            try:
                app.supervise()
            except Exception as exc:
                if isinstance(exc, RuntimeError) and "Another process" in str(exc):
                    # A duplicate supervisor must not stop the existing manager.
                    return
                app._fail(str(exc), log_path=str(app._log_path() or ''))
                import traceback
                traceback.print_exc()
        if args.manager_log:
            log = args.manager_log.resolve()
            if not log.is_relative_to((app.directory / 'logs').resolve()):
                parser.error('Manager log must be inside the local runtime logs directory.')
            with log.open('a', encoding='utf-8', buffering=1) as output, redirect_stdout(output), redirect_stderr(output):
                supervise()
        else:
            supervise()
    elif args.action == 'status':
        print(json.dumps(app.status(), indent=2))
    elif args.action == 'report':
        print(app.write_report())
    elif args.action in {'pause-buys', 'resume-buys'}:
        getattr(app, args.action.replace('-', '_'))()
        print('New buys paused.' if args.action == 'pause-buys' else 'New-buy pause removed; risk limits still apply.')
    else:
        print(json.dumps(getattr(app, args.action)(), indent=2))


if __name__ == '__main__':
    main()
