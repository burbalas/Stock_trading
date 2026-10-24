from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ReadTimeout

from trading_bot.alpaca_integration import (
    ALPACA_REQUEST_TIMEOUT,
    AlpacaMarketDataProvider,
    AlpacaPaperBroker,
    _configure_request_timeouts,
)
from trading_bot.models import Position


def response(payload):
    result = Response()
    result.status_code = 200
    result._content = json.dumps(payload).encode("utf-8")
    return result


class BrokerEfficiencyTests(unittest.TestCase):
    def make_broker(self):
        broker = AlpacaPaperBroker.__new__(AlpacaPaperBroker)
        broker._client = Mock()
        broker.account_id = "previous-account"
        broker.cash = 1000.0
        broker.positions = {"AAPL": Position("AAPL", 2, 100)}
        broker.position_prices = {"AAPL": 110.0}
        return broker

    def assert_previous_account_view(self, broker):
        self.assertEqual(broker.account_id, "previous-account")
        self.assertEqual(broker.cash, 1000.0)
        self.assertEqual(broker.positions, {"AAPL": Position("AAPL", 2, 100)})
        self.assertEqual(broker.position_prices, {"AAPL": 110.0})

    def test_both_client_constructors_install_timeouts_without_network_calls(self):
        with patch("trading_bot.alpaca_integration._alpaca_keys", return_value=("test-key", "test-secret")), \
                patch("trading_bot.alpaca_integration._paper_enabled", return_value=True), \
                patch("trading_bot.alpaca_integration._trading_url_override", return_value=None), \
                patch.object(AlpacaPaperBroker, "refresh"), \
                patch.object(HTTPAdapter, "send") as send:
            provider = AlpacaMarketDataProvider()
            broker = AlpacaPaperBroker()
        send.assert_not_called()
        for client in (provider._client, broker._client):
            with self.subTest(client=type(client).__name__):
                adapter = client._session.get_adapter("https://paper-api.alpaca.markets")
                self.assertEqual(adapter.max_retries.total, 0)
                with patch.object(HTTPAdapter, "send", return_value=response({})) as send:
                    client.get("/offline-test")
                self.assertEqual(send.call_args.kwargs["timeout"], ALPACA_REQUEST_TIMEOUT)
            client._session.close()

    def test_timeout_applies_to_every_paginated_data_request(self):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = _configure_request_timeouts(StockHistoricalDataClient("test-key", "test-secret", raw_data=True))
        original_session = client._session
        pages = [
            response({"bars": {"SPY": [{"c": 100}]}, "next_page_token": "next-page"}),
            response({"bars": {"SPY": [{"c": 101}]}, "next_page_token": None}),
        ]
        with patch.object(HTTPAdapter, "send", side_effect=pages) as send:
            bars = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=["SPY"], timeframe=TimeFrame.Day))
        self.assertIs(client._session, original_session)
        self.assertEqual(bars, {"SPY": [{"c": 100}, {"c": 101}]})
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == ALPACA_REQUEST_TIMEOUT for call in send.call_args_list))
        client._session.close()

    def test_timed_out_write_is_not_automatically_retried(self):
        from alpaca.trading.client import TradingClient

        client = _configure_request_timeouts(TradingClient("test-key", "test-secret", paper=True))
        with patch.object(HTTPAdapter, "send", side_effect=ReadTimeout("response lost")) as send:
            with self.assertRaises(ReadTimeout):
                client.post("/orders", {"client_order_id": "stable-order-id"})
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["timeout"], ALPACA_REQUEST_TIMEOUT)
        client._session.close()

    def test_explicit_finite_session_timeout_is_preserved(self):
        from alpaca.trading.client import TradingClient

        client = _configure_request_timeouts(TradingClient("test-key", "test-secret", paper=True))
        with patch.object(HTTPAdapter, "send", return_value=response({})) as send:
            client._session.get("https://paper-api.alpaca.markets/v2/offline-test", timeout=(1, 2))
        self.assertEqual(send.call_args.kwargs["timeout"], (1, 2))
        client._session.close()

    def test_unsupported_sdk_transport_fails_before_a_request(self):
        with self.assertRaisesRegex(RuntimeError, "bounded network requests"):
            _configure_request_timeouts(SimpleNamespace())

    def test_failed_account_refresh_keeps_previous_complete_view(self):
        broker = self.make_broker()
        broker._client.get_account.side_effect = ReadTimeout("account request timed out")
        with self.assertRaisesRegex(RuntimeError, "account request timed out"):
            broker.refresh()
        self.assert_previous_account_view(broker)
        broker._client.get_all_positions.assert_not_called()

    def test_failed_position_refresh_keeps_previous_complete_view(self):
        broker = self.make_broker()
        broker._client.get_account.return_value = SimpleNamespace(id="new-account", cash="2000")
        broker._client.get_all_positions.side_effect = ReadTimeout("positions request timed out")
        with self.assertRaisesRegex(RuntimeError, "positions request timed out"):
            broker.refresh()
        self.assert_previous_account_view(broker)

    def test_invalid_position_row_does_not_commit_part_of_refresh(self):
        broker = self.make_broker()
        broker._client.get_account.return_value = SimpleNamespace(id="new-account", cash="2000")
        broker._client.get_all_positions.return_value = [
            SimpleNamespace(symbol="MSFT", qty="3", avg_entry_price="200", current_price="210"),
            SimpleNamespace(symbol="NVDA", qty="nan", avg_entry_price="100", current_price="110"),
        ]
        with self.assertRaisesRegex(RuntimeError, "invalid position quantity"):
            broker.refresh()
        self.assert_previous_account_view(broker)

    def test_successful_refresh_replaces_view_and_preserves_fractional_holdings(self):
        broker = self.make_broker()
        broker._client.get_account.return_value = SimpleNamespace(id="same-account", cash="2000")
        broker._client.get_all_positions.return_value = [
            SimpleNamespace(symbol="msft", qty="3.5", avg_entry_price="200", current_price="210"),
            SimpleNamespace(symbol="NVDA", qty="0", avg_entry_price="0", current_price="110"),
        ]
        broker.refresh()
        self.assertEqual(broker.account_id, "same-account")
        self.assertEqual(broker.cash, 2000.0)
        self.assertEqual(broker.positions, {"MSFT": Position("MSFT", 3.5, 200)})
        self.assertEqual(broker.position_prices, {"MSFT": 210.0})


if __name__ == "__main__":
    unittest.main()
