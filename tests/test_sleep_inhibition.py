"""Windows power requests are mocked; no workstation power settings are changed."""
import unittest
from unittest.mock import patch

from trading_bot.paper_app import _keep_system_awake


class SleepInhibitionTests(unittest.TestCase):
    def test_windows_keeps_system_only_awake_and_releases_on_normal_exit(self):
        with patch('trading_bot.paper_app._windows', return_value=True), \
                patch('trading_bot.paper_app._set_thread_execution_state', return_value=0x80000000) as request:
            with _keep_system_awake() as status:
                self.assertTrue(status['sleep_inhibited'])
                self.assertIsNone(status['power_warning'])
                request.assert_called_once_with(0x80000001)
            self.assertEqual([call.args[0] for call in request.call_args_list], [0x80000001, 0x80000000])

    def test_windows_releases_request_after_manager_error(self):
        with patch('trading_bot.paper_app._windows', return_value=True), \
                patch('trading_bot.paper_app._set_thread_execution_state', return_value=0x80000000) as request:
            with self.assertRaisesRegex(RuntimeError, 'manager failed'):
                with _keep_system_awake():
                    raise RuntimeError('manager failed')
            self.assertEqual(request.call_args.args, (0x80000000,))

    def test_failed_power_request_is_visible_without_pretending_sleep_is_blocked(self):
        for result in (0, OSError('unavailable')):
            with self.subTest(result=result), \
                    patch('trading_bot.paper_app._windows', return_value=True), \
                    patch('trading_bot.paper_app._set_thread_execution_state',
                          **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})) as request:
                with _keep_system_awake() as status:
                    self.assertFalse(status['sleep_inhibited'])
                    self.assertIn('failed', status['power_warning'])
                self.assertEqual(request.call_count, 1)

    def test_nonwindows_does_not_call_windows_api(self):
        with patch('trading_bot.paper_app._windows', return_value=False), \
                patch('trading_bot.paper_app._set_thread_execution_state') as request:
            with _keep_system_awake() as status:
                self.assertFalse(status['sleep_inhibited'])
                self.assertIsNone(status['power_warning'])
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
