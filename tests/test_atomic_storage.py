import csv
from datetime import date, datetime, timezone
import errno
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_bot.atomic_storage import _RETRY_DELAYS
from trading_bot.journal import TradeJournal
from trading_bot.models import Order, Side
from trading_bot.runtime import write_heartbeat


def sharing_error(code=5):
    error = PermissionError(errno.EACCES, 'File is temporarily locked')
    error.winerror = code
    return error


class AtomicStorageTests(unittest.TestCase):
    def test_transient_conflicts_preserve_old_state_then_reserve_exactly_once(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_peak_equity(1000)
            previous = journal.state_path.read_bytes()
            real_replace = os.replace
            attempts = []

            def replace(source, destination):
                attempts.append((source, destination))
                self.assertEqual(journal.state_path.read_bytes(), previous)
                if len(attempts) <= 3:
                    raise sharing_error((5, 32, 33)[len(attempts) - 1])
                real_replace(source, destination)

            order = Order('A', Side.BUY, 1, 10, 'entry')
            with patch('trading_bot.atomic_storage.os.replace', side_effect=replace), patch('trading_bot.atomic_storage.time.sleep') as sleep:
                self.assertTrue(journal.reserve_order(order, date(2026, 9, 15)))
            self.assertEqual(sleep.call_count, 3)
            self.assertTrue(all(attempt == attempts[0] for attempt in attempts))
            restarted = TradeJournal(directory)
            self.assertFalse(restarted.reserve_order(order, date(2026, 9, 15)))
            self.assertEqual(len(restarted._read_state()['order_intents']), 1)
            self.assertEqual(restarted._read_state()['peak_equity'], 1000)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_persistent_conflict_leaves_old_state_and_fails_within_retry_budget(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_peak_equity(1000)
            previous = journal.state_path.read_bytes()
            failure = sharing_error()
            with patch('trading_bot.atomic_storage.os.replace', side_effect=failure) as replace, patch('trading_bot.atomic_storage.time.sleep') as sleep:
                with self.assertRaises(PermissionError) as raised:
                    journal.reserve_order(Order('A', Side.BUY, 1, 10, 'entry'), date(2026, 9, 15))
            self.assertIs(raised.exception, failure)
            self.assertEqual(replace.call_count, len(_RETRY_DELAYS) + 1)
            self.assertEqual(sleep.call_count, len(_RETRY_DELAYS))
            self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 6)
            self.assertEqual(journal.state_path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_unrelated_failures_are_not_retried(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_peak_equity(1000)
            previous = journal.state_path.read_bytes()
            for code in (errno.ENOSPC, errno.ENOENT, errno.EXDEV, errno.EACCES):
                with self.subTest(errno=code), patch('trading_bot.atomic_storage.os.replace', side_effect=OSError(code, 'failure')) as replace, patch('trading_bot.atomic_storage.time.sleep') as sleep:
                    with self.assertRaises(OSError):
                        journal.update_peak_equity(1100)
                    replace.assert_called_once()
                    sleep.assert_not_called()
                    self.assertEqual(journal.state_path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_confirmed_fill_retry_cannot_duplicate_cumulative_execution(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            remote = SimpleNamespace(id='broker-1', client_order_id='bot-test', symbol='A', side='buy',
                                     qty='10', filled_qty='10', filled_avg_price='100', status='filled',
                                     filled_at=datetime(2026, 9, 15, 15, tzinfo=timezone.utc))
            real_replace = os.replace
            blocked = set()

            def replace(source, destination):
                if str(destination) not in blocked:
                    blocked.add(str(destination))
                    raise sharing_error(32)
                real_replace(source, destination)

            with patch('trading_bot.atomic_storage.os.replace', side_effect=replace), patch('trading_bot.atomic_storage.time.sleep'):
                self.assertEqual(journal.record_broker_order(remote).quantity, 10)
                self.assertIsNone(TradeJournal(directory).record_broker_order(remote))
            with journal.fills_path.open(newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(float(rows[0]['quantity']), 10)
            self.assertEqual(journal.expected_positions_from_fills(), {'A': 10})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_heartbeat_retry_preserves_valid_old_then_new_json(self):
        with TemporaryDirectory() as directory:
            write_heartbeat(directory, 'running')
            path = Path(directory) / 'heartbeat.json'
            real_replace = os.replace
            attempts = 0

            def replace(source, destination):
                nonlocal attempts
                attempts += 1
                self.assertEqual(json.loads(path.read_text())['status'], 'running')
                if attempts == 1:
                    raise sharing_error()
                real_replace(source, destination)

            with patch('trading_bot.atomic_storage.os.replace', side_effect=replace), patch('trading_bot.atomic_storage.time.sleep'):
                write_heartbeat(directory, 'healthy', equity=1000)
            self.assertEqual(json.loads(path.read_text())['status'], 'healthy')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_cleanup_failure_does_not_hide_primary_storage_error(self):
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            primary = OSError(errno.ENOSPC, 'disk full')
            with patch('trading_bot.atomic_storage.os.replace', side_effect=primary), patch('trading_bot.atomic_storage.Path.unlink', side_effect=sharing_error()):
                with self.assertRaises(OSError) as raised:
                    journal.update_peak_equity(1000)
            self.assertIs(raised.exception, primary)
            self.assertIn('cleanup also failed', raised.exception.__notes__[0])

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing integration')
    def test_real_windows_reader_lock_recovers_without_truncating_state(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with TemporaryDirectory() as directory:
            journal = TradeJournal(directory)
            journal.update_peak_equity(1000)
            # Permit reading/writing but deny deletion/renaming until release.
            handle = kernel.CreateFileW(str(journal.state_path), 0x80000000, 3, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            held = True

            def release(_delay):
                nonlocal held
                self.assertEqual(journal._read_state()['peak_equity'], 1000)
                self.assertTrue(kernel.CloseHandle(handle))
                held = False

            try:
                with patch('trading_bot.atomic_storage.time.sleep', side_effect=release) as sleep:
                    journal.update_peak_equity(1200)
                sleep.assert_called_once()
                self.assertEqual(journal._read_state()['peak_equity'], 1200)
                self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            finally:
                if held:
                    kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
