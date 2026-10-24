"""Desired-state recovery uses disposable runtime data and no brokerage calls."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from requests.exceptions import ConnectionError, Timeout

from trading_bot.journal import TradeJournal
from trading_bot.paper_app import PaperApplication, _network_error, _read, _write


class PaperRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.directory = self.root / 'runtime'
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')})
        environment.start()
        self.addCleanup(environment.stop)
        TradeJournal(self.project / 'var').bind_account('paper-test')
        self.app = PaperApplication(self.project, self.directory)
        self.app.ensure_runtime()
        _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': 'paper-test'})

    def test_sign_in_launch_does_not_need_network_or_clear_a_buy_pause(self):
        self.app.pause_buys()
        with patch.object(self.app, '_launch_background') as launch, \
                patch.object(self.app, 'refresh_broker') as broker:
            self.app.resume()
        launch.assert_called_once()
        broker.assert_not_called()
        self.assertTrue((self.directory / 'STOP_BUYS').exists())
        self.assertTrue(_read(self.directory / 'control.json')['enabled'])

    def test_sign_in_does_not_recover_changed_account(self):
        _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': 'another-account'})
        with patch.object(self.app, '_launch_background') as launch:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                self.app.resume()
        launch.assert_not_called()
        self.assertEqual(TradeJournal(self.directory)._read_state()['account_id'], 'paper-test')

    def test_network_preflight_retries_before_any_worker_then_recovers(self):
        process = Mock(returncode=0, pid=123)
        process.poll.return_value = 0

        def spawn(*args, **kwargs):
            self.app.stop()
            return process

        with patch.object(self.app, 'refresh_broker', side_effect=[ConnectionError('offline'), Timeout('read timed out'),
                                                                {'reconciliation_ok': True}]) as broker, \
                patch.object(self.app, '_wait_for_retry', return_value=True) as wait, \
                patch('trading_bot.paper_app.subprocess.Popen', side_effect=spawn) as worker:
            self.app.supervise()
        self.assertEqual(broker.call_count, 3)
        worker.assert_called_once()
        self.assertEqual([call.args[0] for call in wait.call_args_list], [5, 10])
        self.assertEqual(_read(self.directory / 'supervisor.json')['status'], 'stopped')
        self.assertFalse(self.app._supervising)
        self.assertFalse(self.app.status()['supervisor']['manager_running'])

    def test_stop_ends_network_recovery_without_launching_worker(self):
        def request_stop(seconds):
            state = self.app.status()['supervisor']
            self.assertEqual(state['status'], 'waiting_for_network')
            self.assertTrue(state['manager_running'])
            self.assertFalse(state['worker_running'])
            self.app.stop()
            return False

        with patch.object(self.app, 'refresh_broker', side_effect=ConnectionError('offline')), \
                patch.object(self.app, '_wait_for_retry', side_effect=request_stop), \
                patch('trading_bot.paper_app.subprocess.Popen') as worker:
            self.app.supervise()
        worker.assert_not_called()
        self.assertTrue((self.directory / 'STOP').exists())
        self.assertFalse(_read(self.directory / 'control.json')['enabled'])

    def test_authentication_and_reconciliation_errors_never_auto_retry(self):
        for error in (RuntimeError('401 unauthorized'), RuntimeError('Account reconciliation failed')):
            with self.subTest(error=error):
                (self.directory / 'STOP').unlink(missing_ok=True)
                _write(self.directory / 'control.json', {'enabled': True, 'expected_account_id': 'paper-test'})
                with patch.object(self.app, 'refresh_broker', side_effect=error), \
                        patch.object(self.app, '_wait_for_retry') as wait, \
                        patch('trading_bot.paper_app.subprocess.Popen') as worker:
                    self.app.supervise()
                wait.assert_not_called()
                worker.assert_not_called()
                self.assertEqual(_read(self.directory / 'supervisor.json')['status'], 'failed')
                self.assertFalse(_read(self.directory / 'control.json')['enabled'])

    def test_worker_network_failure_restarts_only_after_fresh_preflight(self):
        process = Mock(returncode=1, pid=123)
        process.poll.return_value = 1
        launches = []

        def spawn(*args, **kwargs):
            launches.append(1)
            if len(launches) == 1:
                _write(self.directory / 'heartbeat.json', {'updated_at': datetime.now(timezone.utc).isoformat(),
                                                         'status': 'failed', 'error': 'ConnectionError: network is unreachable'})
            else:
                self.app.stop()
            return process

        with patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': True}) as broker, \
                patch.object(self.app, '_wait_for_retry', return_value=True) as wait, \
                patch('trading_bot.paper_app.subprocess.Popen', side_effect=spawn):
            self.app.supervise()
        self.assertEqual(len(launches), 2)
        self.assertEqual(broker.call_count, 2)
        wait.assert_called_once_with(5)

    def test_report_failure_does_not_kill_risk_monitoring(self):
        process = Mock(returncode=0, pid=123)
        process.poll.side_effect = [None, 0]
        _write(self.directory / 'heartbeat.json', {'updated_at': datetime.now(timezone.utc).isoformat(),
                                                 'status': 'waiting_for_open'})
        with patch.object(self.app, 'refresh_broker', return_value={'reconciliation_ok': True}), \
                patch.object(self.app, 'write_report', side_effect=ValueError('invalid old CSV')), \
                patch('trading_bot.paper_app.subprocess.Popen', return_value=process) as worker, \
                patch('trading_bot.paper_app.time.sleep', side_effect=lambda seconds: self.app.stop()):
            self.app.supervise()
        worker.assert_called_once()
        self.assertEqual(_read(self.directory / 'supervisor.json')['status'], 'stopped')

    def test_missing_enabled_manager_is_an_explicit_recovery_warning(self):
        self.app._state('running')
        result = self.app.status()['supervisor']
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['recovery_required'])
        self.assertTrue(result['attention_required'])
        self.assertIn('left enabled', result['last_error'])

    def test_process_presence_does_not_hide_stale_manager_or_worker(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write(self.directory / 'supervisor.json', {'status': 'running', 'updated_at': old})
        _write(self.directory / 'heartbeat.json', {'status': 'healthy', 'updated_at': old})
        with patch.object(self.app, '_manager_alive', return_value=True), \
                patch.object(self.app, '_worker_alive', return_value=True):
            result = self.app.status()
        self.assertTrue(result['supervisor']['manager_stale'])
        self.assertTrue(result['supervisor']['attention_required'])
        self.assertEqual(result['operator']['state'], 'stale')
        self.assertIn('unverified', result['supervisor']['last_error'])

    def test_network_retry_delay_is_capped_and_stop_is_checked_before_sleep(self):
        with patch.object(self.app, '_wait_for_retry', return_value=True) as wait:
            self.app._network_recovery(ConnectionError('offline'), 100)
        wait.assert_called_once_with(300)
        self.app.stop()
        with patch('trading_bot.paper_app.time.sleep') as sleep:
            self.assertFalse(self.app._wait_for_retry(300))
        sleep.assert_not_called()

    def test_transport_classification_excludes_account_and_authentication_errors(self):
        for error in (ConnectionError('offline'), Timeout('timeout'), 'ReadTimeout: retry later',
                      'Max retries exceeded with url: /account'):
            self.assertTrue(_network_error(error), str(error))
        for error in (RuntimeError('401 Unauthorized'), ValueError('Invalid quantity'),
                      'Account differs', 'Reconciliation failed', 'No active market data subscription'):
            self.assertFalse(_network_error(error), str(error))

    def test_disabled_manager_never_requests_sleep_inhibition(self):
        self.app.stop()
        with patch('trading_bot.paper_app._keep_system_awake') as awake:
            self.app.supervise()
        awake.assert_not_called()
        self.assertFalse(self.app.status()['supervisor']['sleep_inhibited'])


if __name__ == '__main__':
    unittest.main()
