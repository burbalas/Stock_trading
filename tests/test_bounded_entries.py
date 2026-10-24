from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, ReplaceOrderRequest, StopOrderRequest

from trading_bot.alpaca_integration import AlpacaPaperBroker
from trading_bot.models import Order, Position, Side


class NotFound(Exception):
    status_code = 404


def remote(**changes):
    fields = dict(id="remote-1", client_order_id="bot-entry", symbol="AAPL", side="buy", qty="10",
                  filled_qty="0", filled_avg_price=None, status="canceled", type="limit",
                  time_in_force="ioc", limit_price="100.05", extended_hours=False,
                  submitted_at=datetime(2026, 10, 22, 15, tzinfo=timezone.utc), filled_at=None)
    fields.update(changes)
    return SimpleNamespace(**fields)


def broker(limit_bps=5):
    adapter = AlpacaPaperBroker.__new__(AlpacaPaperBroker)
    adapter._configure_entry_policy(limit_bps)
    adapter._client = Mock()
    adapter._order_side = OrderSide
    adapter._order_type = OrderType
    adapter._time_in_force = TimeInForce
    adapter._market_order_request = MarketOrderRequest
    adapter._limit_order_request = LimitOrderRequest
    adapter._stop_order_request = StopOrderRequest
    adapter._replace_order_request = ReplaceOrderRequest
    adapter.refresh = Mock()
    adapter.positions = {"AAPL": Position("AAPL", 10, 100)}
    adapter.fills = []
    adapter.last_submitted_order = None
    return adapter


class BoundedEntryTests(unittest.TestCase):
    def setUp(self):
        self.order = Order("AAPL", Side.BUY, 10, 100, "bounded entry", "bot-entry")

    def test_price_cap_rounds_down_without_exceeding_allowance(self):
        adapter = broker()
        self.assertEqual(adapter.maximum_entry_price(100), 100.05)
        self.assertEqual(adapter.maximum_entry_price(100.009), 100.05)
        self.assertEqual(adapter.maximum_entry_price(0.4321), 0.4323)
        self.assertEqual(adapter.maximum_entry_price(0.9999), 1.0)
        adapter._configure_entry_policy(0)
        self.assertEqual(adapter.maximum_entry_price(100.009), 100.0)

    def test_invalid_allowances_and_prices_are_rejected_before_network_access(self):
        adapter = broker()
        for allowance in (True, -1, 101, float("nan"), float("inf"), "5"):
            with self.subTest(allowance=allowance), self.assertRaises(ValueError):
                adapter._configure_entry_policy(allowance)
        for ask in (True, 0, -1, float("nan"), float("inf"), "100", 0.00000001):
            with self.subTest(ask=ask), self.assertRaises(ValueError):
                adapter.maximum_entry_price(ask)
        adapter._client.submit_order.assert_not_called()

    def test_configured_buy_uses_regular_session_ioc_limit_without_fallback(self):
        adapter = broker()
        adapter._client.get_order_by_client_id.side_effect = NotFound()
        adapter._client.submit_order.return_value = remote()
        self.assertIsNone(adapter.submit(self.order))
        request = adapter._client.submit_order.call_args.args[0]
        self.assertIsInstance(request, LimitOrderRequest)
        self.assertEqual(request.to_request_fields()["type"], OrderType.LIMIT)
        self.assertEqual(request.time_in_force, TimeInForce.IOC)
        self.assertEqual(request.limit_price, 100.05)
        self.assertIs(request.extended_hours, False)
        adapter._client.submit_order.assert_called_once()
        self.assertEqual(adapter.fills, [])

    def test_disabled_policy_preserves_existing_paper_market_behavior(self):
        adapter = broker(None)
        adapter._client.get_order_by_client_id.side_effect = NotFound()
        adapter._client.submit_order.return_value = remote(type="market", time_in_force="day", limit_price=None)
        adapter.submit(self.order)
        self.assertIsInstance(adapter._client.submit_order.call_args.args[0], MarketOrderRequest)
        self.assertEqual(adapter.maximum_entry_price(100.009), 100.009)

    def test_risk_sell_remains_market_and_checks_current_long_position(self):
        adapter = broker()
        adapter._client.get_order_by_client_id.side_effect = NotFound()
        adapter._client.submit_order.return_value = remote(side="sell", type="market", time_in_force="day")
        adapter.submit(Order("AAPL", Side.SELL, 10, 100, "risk exit", "bot-entry"))
        self.assertIsInstance(adapter._client.submit_order.call_args.args[0], MarketOrderRequest)
        adapter.refresh.assert_called()

    def test_recovered_client_id_must_still_have_the_bounded_policy(self):
        for changes in (dict(type="market"), dict(time_in_force="day"), dict(limit_price="100.06"),
                        dict(limit_price=None), dict(limit_price="NaN"), dict(extended_hours=True),
                        dict(client_order_id="somebody-else"), dict(filled_qty="11", filled_avg_price="100"),
                        dict(filled_qty="1", filled_avg_price="100.06")):
            with self.subTest(changes=changes):
                adapter = broker()
                adapter._client.get_order_by_client_id.return_value = remote(**changes)
                with self.assertRaisesRegex(RuntimeError, "entry"):
                    adapter.submit(self.order)
                adapter._client.submit_order.assert_not_called()

    def test_terminal_partial_fill_uses_only_confirmed_quantity_and_average(self):
        adapter = broker()
        partial = remote(filled_qty="3", filled_avg_price="100.025", status="canceled")
        adapter._client.get_order_by_client_id.return_value = partial
        execution = adapter.submit(self.order)
        self.assertEqual(execution.quantity, 3)
        self.assertEqual(execution.price, 100.025)
        self.assertAlmostEqual(execution.value, 300.075)
        adapter._client.submit_order.assert_not_called()

    def test_lost_submit_response_recovers_same_order_with_no_second_write(self):
        adapter = broker()
        accepted = remote(filled_qty="10", filled_avg_price="100.04", status="filled")
        adapter._client.get_order_by_client_id.side_effect = [NotFound(), accepted]
        adapter._client.submit_order.side_effect = TimeoutError("response lost")
        self.assertEqual(adapter.submit(self.order).quantity, 10)
        adapter._client.submit_order.assert_called_once()
        self.assertEqual(adapter._client.get_order_by_client_id.call_args_list[0].args, ("bot-entry",))
        self.assertEqual(adapter._client.get_order_by_client_id.call_args_list[1].args, ("bot-entry",))

    def test_submission_failure_does_not_fall_back_to_market(self):
        adapter = broker()
        adapter._client.get_order_by_client_id.side_effect = NotFound()
        adapter._client.submit_order.side_effect = RuntimeError("IOC rejected")
        with self.assertRaisesRegex(RuntimeError, "IOC rejected"):
            adapter.submit(self.order)
        adapter._client.submit_order.assert_called_once()
        self.assertIsInstance(adapter._client.submit_order.call_args.args[0], LimitOrderRequest)

    @patch("trading_bot.alpaca_integration.time.sleep")
    def test_polling_cannot_replace_the_bounded_order_identity(self, _sleep):
        adapter = broker()
        adapter._client.get_order_by_client_id.side_effect = [remote(status="accepted"), remote(qty="11")]
        with self.assertRaisesRegex(RuntimeError, "different order"):
            adapter.submit(self.order)
        adapter._client.submit_order.assert_not_called()

    def test_authorization_hook_blocks_new_entry_before_write(self):
        adapter = broker()
        adapter._client.get_order_by_client_id.side_effect = NotFound()
        adapter._validate_trading_request = Mock(side_effect=RuntimeError("deployment disabled"))
        with self.assertRaisesRegex(RuntimeError, "deployment disabled"):
            adapter.submit(self.order)
        adapter._validate_trading_request.assert_called_once_with("submit", order=self.order)
        adapter._client.submit_order.assert_not_called()

    def test_authorization_hook_blocks_new_protection_before_write(self):
        adapter = broker()
        adapter._client.get_order_by_client_id.side_effect = NotFound()
        adapter._validate_trading_request = Mock(side_effect=RuntimeError("deployment disabled"))
        with self.assertRaisesRegex(RuntimeError, "deployment disabled"):
            adapter.submit_protective_stop("AAPL", 10, 90, "bot-protection")
        adapter._client.submit_order.assert_not_called()
        self.assertEqual(adapter._validate_trading_request.call_args.args, ("protect",))
        self.assertEqual(adapter._validate_trading_request.call_args.kwargs["order"].side, Side.SELL)

    def test_authorization_hook_blocks_stop_replacement_before_write(self):
        adapter = broker()
        adapter._validate_trading_request = Mock(side_effect=RuntimeError("deployment disabled"))
        with self.assertRaisesRegex(RuntimeError, "deployment disabled"):
            adapter.replace_stop_order("broker-stop", 95)
        adapter._validate_trading_request.assert_called_once_with("replace")
        adapter._client.replace_order_by_id.assert_not_called()

    def test_authorization_hook_blocks_sell_cancellation_before_write(self):
        adapter = broker()
        adapter.open_orders = Mock(return_value=[remote(side="sell", type="stop")])
        adapter._validate_trading_request = Mock(side_effect=RuntimeError("deployment disabled"))
        with self.assertRaisesRegex(RuntimeError, "deployment disabled"):
            adapter.cancel_open_sell_orders("AAPL")
        adapter._validate_trading_request.assert_called_once_with("cancel")
        adapter._client.cancel_order_by_id.assert_not_called()

    def test_authorization_hook_blocks_buy_cancellation_without_swallowing_rejection(self):
        adapter = broker()
        adapter.open_orders = Mock(return_value=[remote(status="new")])
        adapter.closed_orders = Mock(return_value=[])
        adapter._validate_trading_request = Mock(side_effect=RuntimeError("deployment disabled"))
        with self.assertRaisesRegex(RuntimeError, "deployment disabled"):
            adapter.cancel_open_buy_orders({"bot-entry"})
        adapter._validate_trading_request.assert_called_once_with("cancel")
        adapter._client.cancel_order_by_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
