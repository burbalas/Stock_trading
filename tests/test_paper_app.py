import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_bot.journal import TradeJournal
from trading_bot.paper_app import PaperApplication, _read, _write, candidate_arguments, protected_symbols
from trading_bot.paper_control import paper_execution_lock


class PaperApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.directory = self.root / 'runtime'
        self.app = PaperApplication(self.project, self.directory)
        journal = TradeJournal(self.project / 'var')
        journal.bind_account('paper-test')
        journal.update_peak_equity(1000)
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app.ensure_runtime()

    def test_runtime_keeps_old_account_and_terminal_journal_resolves_same_data(self):
        self.assertEqual(TradeJournal(self.project / 'var').directory, self.directory)
        self.assertEqual(TradeJournal(self.directory)._read_state()['account_id'], 'paper-test')
        self.assertEqual(self.app.status()['operator']['state'], 'missing')

    def test_start_preflights_and_launches_continuous_worker_without_credentials_in_command(self):
        with patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': True}), \
             patch.object(self.app, '_launch_manager') as spawn:
            result = self.app.start()
            self.assertEqual(result['supervisor']['status'], 'starting')
            arguments, options = spawn.call_args
            self.assertIn('supervise', arguments[0])
            self.assertIn('PYTHONUTF8', arguments[2])
            self.assertEqual(_read(self.directory / 'control.json')['expected_account_id'], 'paper-test')
            self.assertTrue(_read(self.directory / 'control.json')['enabled'])
            self.assertFalse((self.directory / 'STOP').exists())
            self.app.start()
            spawn.assert_called_once()

    def test_failed_preflight_or_spawn_never_leaves_enabled_unobserved_worker(self):
        with patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': False}), \
             patch.object(self.app, '_launch_manager') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                self.app.start()
            spawn.assert_not_called()
        with patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': True}), \
             patch.object(self.app, '_launch_manager', side_effect=OSError('cannot launch')):
            with self.assertRaises(OSError):
                self.app.start()
        self.assertFalse(_read(self.directory / 'control.json')['enabled'])
        self.assertEqual(self.app.status()['supervisor']['status'], 'failed')

    def test_stop_persists_and_buy_pause_cannot_clear_drawdown_latch(self):
        _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': 'paper-test'})
        self.app.pause_buys()
        self.app.stop()
        self.assertFalse(_read(self.directory / 'control.json')['enabled'])
        self.assertTrue((self.directory / 'STOP').exists())
        self.assertTrue(self.app.status()['paused'])
        journal = TradeJournal(self.directory)
        journal.update_drawdown_state(700, .15)
        with self.assertRaisesRegex(RuntimeError, 'latched'):
            self.app.resume_buys()
        self.assertTrue((self.directory / 'STOP_BUYS').exists())

    def test_account_change_refused_before_remote_history_is_imported(self):
        broker = Mock(account_id='different-account')
        with patch('trading_bot.alpaca_integration.AlpacaPaperBroker', return_value=broker), \
             patch('trading_bot.reconcile.sync_broker_orders') as sync:
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                self.app.refresh_broker()
            sync.assert_not_called()
        self.assertEqual(TradeJournal(self.directory)._read_state()['account_id'], 'paper-test')

    def test_single_user_lock_prevents_other_directory_submitter(self):
        code = 'from trading_bot.paper_control import paper_execution_lock\nwith paper_execution_lock(): pass'
        with paper_execution_lock():
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Another process', result.stderr)
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_logs_are_bounded_and_cannot_read_outside_runtime_log_folder(self):
        logs = self.directory / 'logs'
        logs.mkdir(exist_ok=True)
        path = logs / 'paper.log'
        path.write_text('x' * 70000)
        self.app._state('failed', log_path=str(path))
        self.assertEqual(len(self.app.logs()), 48000)
        outside = self.root / 'private.txt'
        outside.write_text('private')
        self.app._state('failed', log_path=str(outside))
        self.assertNotIn('private', self.app.logs())

    def test_old_starting_or_running_status_is_not_mistaken_for_live_manager(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        for state in ('starting', 'running', 'restarting'):
            _write(self.directory / 'supervisor.json', {'status': state, 'updated_at': old})
            self.assertEqual(self.app.status()['supervisor']['status'], 'stopped')

    def test_supervisor_limits_restarts_and_never_restarts_after_user_stop(self):
        logs = self.directory / 'logs'
        logs.mkdir(exist_ok=True)
        _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': 'paper-test'})
        process = Mock(returncode=2, pid=123)
        process.poll.return_value = 2
        with patch('trading_bot.paper_app.subprocess.Popen', return_value=process) as spawn, \
             patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': True}), \
             patch('trading_bot.paper_app.time.sleep'):
            self.app.supervise()
        self.assertEqual(spawn.call_count, 3)
        self.assertFalse(_read(self.directory / 'control.json')['enabled'])
        self.assertTrue((self.directory / 'STOP').exists())
        self.assertEqual(_read(self.directory / 'supervisor.json')['status'], 'failed')
        with patch('trading_bot.paper_app.subprocess.Popen') as spawn:
            self.app.supervise()
            spawn.assert_not_called()

    def test_candidate_stays_paper_and_uses_same_risk_budget(self):
        args = candidate_arguments(self.project, self.directory)
        self.assertIn('--continuous', args)
        self.assertEqual(args[args.index('--max-order-pct') + 1], '0.10')
        self.assertEqual(args[args.index('--trailing-stop-pct') + 1], '0.10')
        self.assertEqual(args[args.index('--max-drawdown-stop-pct') + 1], '0.15')
        self.assertEqual(args[args.index('--journal-dir') + 1], str(self.directory))

    def test_protection_requires_active_broker_stop_and_full_remaining_quantity(self):
        position = {'symbol': 'A', 'quantity': 10}
        stop = {'symbol': 'A', 'side': 'sell', 'type': 'stop', 'managed': True, 'status': 'new',
                'quantity': 10, 'filled_quantity': 0, 'stop_price': 90}
        self.assertEqual(protected_symbols([position], [stop]), ['A'])
        for changes in ({'status': 'pending_new'}, {'status': 'canceled'}, {'managed': False},
                        {'quantity': 9}, {'filled_quantity': 1}, {'stop_price': None}, {'type': 'market'}):
            self.assertEqual(protected_symbols([position], [dict(stop, **changes)]), [])
        self.assertEqual(protected_symbols([{'symbol': 'A', 'quantity': 7}],
                                           [dict(stop, filled_quantity=3, status='partially_filled')]), ['A'])

    def test_logon_resume_cannot_undo_user_stop(self):
        for control, stop in [({'enabled': False}, False), ({'enabled': True, 'expected_account_id': 'paper-test'}, True)]:
            _write(self.directory / 'control.json', control)
            if stop:
                (self.directory / 'STOP').touch()
            with patch.object(self.app, '_launch_background') as start:
                self.app.resume()
                start.assert_not_called()
        (self.directory / 'STOP').unlink()
        with patch.object(self.app, '_launch_background') as start:
            self.app.resume()
            start.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows shell launch')
    def test_windows_launch_detaches_without_inheriting_terminal_output_pipes(self):
        with patch('trading_bot.paper_app.subprocess.run') as run:
            self.app._launch_manager([sys.executable, '-m', 'trading_bot.paper_app', 'supervise'],
                                     self.directory / 'logs' / 'manager.log', os.environ.copy())
        script = run.call_args.args[0][-1]
        self.assertIn('Start-Process -WindowStyle Hidden', script)
        self.assertNotIn('RedirectStandard', script)
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_real_supervisor_stops_offline_worker_without_restart(self):
        package = self.project / 'trading_bot'
        package.mkdir()
        (package / '__init__.py').write_text('')
        # A disposable CLI implements only a heartbeat and STOP check. It has
        # no brokerage imports, so this verifies real process lifecycle safely.
        (package / 'cli.py').write_text(
            "import sys,time,json\nfrom pathlib import Path\n"
            "from datetime import datetime,timezone\n"
            "p=Path(sys.argv[sys.argv.index('--journal-dir')+1])\n"
            "(p/'offline-ready').touch()\n"
            "print('offline worker ready',flush=True)\n"
            "while not (p/'STOP').exists(): time.sleep(.05)\n"
            "print('offline worker stopped',flush=True)\n", encoding='utf-8')
        _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': 'paper-test'})
        failures = []
        preflight = patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': True})
        preflight.start()
        self.addCleanup(preflight.stop)

        def supervise():
            try:
                self.app.supervise()
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=supervise)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not (self.directory / 'offline-ready').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((self.directory / 'offline-ready').exists(), failures)
            self.app.stop()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures)
            self.assertFalse(_read(self.directory / 'control.json')['enabled'])
            self.assertEqual(_read(self.directory / 'supervisor.json')['status'], 'stopped')
            self.assertIn('offline worker stopped', self.app.logs())
            self.assertEqual(len(list((self.directory / 'logs').glob('paper-*.log'))), 1)
        finally:
            (self.directory / 'STOP').touch()
            thread.join(10)


if __name__ == '__main__':
    unittest.main()
