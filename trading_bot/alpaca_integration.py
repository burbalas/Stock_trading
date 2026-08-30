from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import Candle, Fill, Order, PortfolioSnapshot, Position, Side
from .time_utils import LOCAL_TZ


def _load_local_env() -> None:
    for env_path in (Path(".env"), Path(".env.local")):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _load_alpaca_modules():
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.requests import ReplaceOrderRequest
        from alpaca.trading.requests import StopOrderRequest
    except ImportError as exc:
        raise RuntimeError(
            "Alpaca support requires the official SDK. Install it with: pip install alpaca-py"
        ) from exc

    return {
        "StockHistoricalDataClient": StockHistoricalDataClient,
        "StockBarsRequest": StockBarsRequest,
        "TimeFrame": TimeFrame,
        "TradingClient": TradingClient,
        "OrderSide": OrderSide,
        "OrderType": OrderType,
        "QueryOrderStatus": QueryOrderStatus,
        "TimeInForce": TimeInForce,
        "GetOrdersRequest": GetOrdersRequest,
        "MarketOrderRequest": MarketOrderRequest,
        "ReplaceOrderRequest": ReplaceOrderRequest,
        "StopOrderRequest": StopOrderRequest,
    }


def _alpaca_keys() -> tuple[str, str]:
    _load_local_env()
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError(
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY before using Alpaca integration."
        )
    return api_key, secret_key


def _paper_enabled() -> bool:
    _load_local_env()
    return os.getenv("ALPACA_PAPER", "true").strip().lower() not in {"0", "false", "no"}


def _trading_url_override() -> str | None:
    _load_local_env()
    base_url = os.getenv("ALPACA_BASE_URL")
    if not base_url:
        return None
    return base_url.rstrip("/").removesuffix("/v2")


def _alpaca_error(exc: Exception) -> RuntimeError:
    return RuntimeError(f"Alpaca API request failed: {exc}")


class AlpacaMarketDataProvider:
    def __init__(self, days: int = 180, feed: str = "iex", exclude_date: date | None = None) -> None:
        modules = _load_alpaca_modules()
        api_key, secret_key = _alpaca_keys()
        self.days = days
        self.feed = feed
        self.exclude_date = exclude_date
        self._request_cls = modules["StockBarsRequest"]
        self._timeframe = modules["TimeFrame"]
        self._client = modules["StockHistoricalDataClient"](api_key, secret_key)

    def load(self, symbols: list[str]) -> dict[str, list[Candle]]:
        normalized = [symbol.upper() for symbol in symbols]
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.days)
        request = self._request_cls(
            symbol_or_symbols=normalized,
            timeframe=self._timeframe.Day,
            start=start,
            end=end,
            feed=self.feed,
        )
        bars = self._client.get_stock_bars(request)
        raw_data = getattr(bars, "data", {})

        data: dict[str, list[Candle]] = {symbol: [] for symbol in normalized}
        for symbol, rows in raw_data.items():
            data[symbol.upper()] = [
                Candle(
                    date=bar.timestamp.date(),
                    symbol=symbol.upper(),
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=int(bar.volume),
                )
                for bar in rows
                if self.exclude_date is None or bar.timestamp.date() != self.exclude_date
            ]

        return {symbol: sorted(rows, key=lambda candle: candle.date) for symbol, rows in data.items()}


class AlpacaPaperBroker:
    def __init__(self) -> None:
        modules = _load_alpaca_modules()
        api_key, secret_key = _alpaca_keys()
        if not _paper_enabled():
            raise RuntimeError("This adapter only supports Alpaca paper trading. Set ALPACA_PAPER=true.")

        self._order_side = modules["OrderSide"]
        self._order_type = modules["OrderType"]
        self._query_order_status = modules["QueryOrderStatus"]
        self._time_in_force = modules["TimeInForce"]
        self._get_orders_request = modules["GetOrdersRequest"]
        self._market_order_request = modules["MarketOrderRequest"]
        self._replace_order_request = modules["ReplaceOrderRequest"]
        self._stop_order_request = modules["StopOrderRequest"]
        self._client = modules["TradingClient"](
            api_key,
            secret_key,
            paper=True,
            url_override=_trading_url_override(),
        )
        self.cash = 0.0
        self.positions: dict[str, Position] = {}
        self.position_prices: dict[str, float] = {}
        self.fills: list[Fill] = []
        self.account_id: str | None = None
        self.refresh()

    def refresh(self) -> None:
        try:
            account = self._client.get_account()
        except Exception as exc:
            raise _alpaca_error(exc)
        self.account_id = str(getattr(account, "id", "") or "") or None
        self.cash = float(account.cash)
        self.positions = {}
        self.position_prices = {}

        try:
            remote_positions = self._client.get_all_positions()
        except Exception as exc:
            raise _alpaca_error(exc)

        for remote_position in remote_positions:
            quantity = int(float(remote_position.qty))
            if quantity <= 0:
                continue
            symbol = remote_position.symbol.upper()
            self.positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                average_cost=float(remote_position.avg_entry_price),
            )
            self.position_prices[symbol] = self._remote_position_price(remote_position, quantity)

    def _remote_position_price(self, remote_position, quantity: int) -> float:
        raw_price = getattr(remote_position, "current_price", None)
        if raw_price not in (None, ""):
            return float(raw_price)
        market_value = float(getattr(remote_position, "market_value", 0.0) or 0.0)
        return market_value / quantity if quantity else 0.0

    def submit(self, order: Order, trade_date: date | None = None) -> Fill | None:
        if order.quantity <= 0 or order.side is Side.HOLD:
            return None

        side = self._order_side.BUY if order.side is Side.BUY else self._order_side.SELL
        request = self._market_order_request(
            symbol=order.symbol,
            qty=order.quantity,
            side=side,
            time_in_force=self._time_in_force.DAY,
            client_order_id=order.client_order_id,
        )
        try:
            submitted_order = self._client.submit_order(request)
        except Exception as exc:
            raise _alpaca_error(exc)

        fill_price = self._filled_average_price(submitted_order)
        if fill_price is None and order.client_order_id:
            fill_price = self._poll_filled_average_price(order.client_order_id)
        self.refresh()
        if fill_price is None:
            fill_price = self._position_average_price(order) or order.price

        fill = Fill(
            date=trade_date or date.today(),
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            value=order.quantity * fill_price,
            reason=f"submitted to Alpaca paper: {order.reason}",
        )
        self.fills.append(fill)
        return fill

    def submit_protective_stop(self, symbol: str, quantity: int, stop_price: float, client_order_id: str):
        if quantity <= 0 or stop_price <= 0:
            return None
        request = self._stop_order_request(
            symbol=symbol,
            qty=quantity,
            side=self._order_side.SELL,
            type=self._order_type.STOP,
            time_in_force=self._time_in_force.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        try:
            return self._client.submit_order(request)
        except Exception as exc:
            raise _alpaca_error(exc)

    def replace_stop_order(self, order_id, stop_price: float, quantity: int | None = None):
        request = self._replace_order_request(
            qty=quantity,
            stop_price=round(stop_price, 2),
        )
        try:
            return self._client.replace_order_by_id(order_id, request)
        except Exception as exc:
            raise _alpaca_error(exc)

    def cancel_open_sell_orders(self, symbol: str) -> int:
        canceled = 0
        for order in self.open_orders([symbol]):
            side = getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))
            if str(side).lower() != Side.SELL.value:
                continue
            try:
                self._client.cancel_order_by_id(order.id)
            except Exception as exc:
                raise _alpaca_error(exc)
            canceled += 1
        return canceled

    def _poll_filled_average_price(self, client_order_id: str) -> float | None:
        if not hasattr(self._client, "get_order_by_client_id"):
            return None
        for _ in range(4):
            time.sleep(0.5)
            try:
                remote_order = self._client.get_order_by_client_id(client_order_id)
            except Exception:
                continue
            fill_price = self._filled_average_price(remote_order)
            if fill_price is not None:
                return fill_price
        return None

    def _filled_average_price(self, remote_order) -> float | None:
        raw_price = getattr(remote_order, "filled_avg_price", None)
        if raw_price in (None, ""):
            return None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _position_average_price(self, order: Order) -> float | None:
        if order.side is not Side.BUY:
            return None
        position = self.positions.get(order.symbol)
        if not position or position.quantity <= 0 or position.average_cost <= 0:
            return None
        return position.average_cost

    def open_client_order_ids(self, symbols: list[str] | None = None) -> set[str]:
        request = self._get_orders_request(
            status=self._query_order_status.OPEN,
            symbols=symbols,
        )
        try:
            orders = self._client.get_orders(request)
        except Exception as exc:
            raise _alpaca_error(exc)
        return {
            str(order.client_order_id)
            for order in orders
            if getattr(order, "client_order_id", None)
        }

    def open_orders(self, symbols: list[str] | None = None):
        request = self._get_orders_request(
            status=self._query_order_status.OPEN,
            symbols=symbols,
        )
        try:
            return self._client.get_orders(request)
        except Exception as exc:
            raise _alpaca_error(exc)

    def closed_orders(self, symbols: list[str] | None = None):
        request = self._get_orders_request(
            status=self._query_order_status.CLOSED,
            symbols=symbols,
        )
        try:
            return self._client.get_orders(request)
        except Exception as exc:
            raise _alpaca_error(exc)

    def market_clock(self):
        try:
            return self._client.get_clock()
        except Exception as exc:
            raise _alpaca_error(exc)

    def trading_date(self) -> date:
        clock = self.market_clock()
        return clock.timestamp.astimezone(LOCAL_TZ).date()

    def snapshot(self, prices: dict[str, float], current_date: date | None = None) -> PortfolioSnapshot:
        try:
            account = self._client.get_account()
        except Exception as exc:
            raise _alpaca_error(exc)
        self.cash = float(account.cash)
        equity = float(account.equity)
        return PortfolioSnapshot(
            date=current_date or date.today(),
            cash=self.cash,
            positions_value=equity - self.cash,
            equity=equity,
        )
