from __future__ import annotations

import os
import math
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path
from urllib.parse import urlsplit

from .models import Candle, Fill, Order, PortfolioSnapshot, Position, Side
from .time_utils import MARKET_TZ


TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}
ALPACA_REQUEST_TIMEOUT = (5.0, 30.0)  # Connect and socket-read timeouts, in seconds.


def _configure_request_timeouts(client):
    """Bound SDK network waits while preserving its pooled session and retries.

    alpaca-py 0.44 does not expose a request timeout in either client constructor.
    Its RESTClient uses a requests session for every request, including paginated
    data. A transport adapter supplies the missing connect/read limits without
    retrying a timed-out order submission (which may already have been accepted).
    """
    from requests.adapters import HTTPAdapter

    class TimeoutAdapter(HTTPAdapter):
        def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
            return super().send(
                request, stream=stream,
                timeout=ALPACA_REQUEST_TIMEOUT if timeout is None else timeout,
                verify=verify, cert=cert, proxies=proxies,
            )

    session = getattr(client, "_session", None)
    if session is None or not callable(getattr(session, "mount", None)):
        raise RuntimeError("The installed Alpaca SDK cannot configure bounded network requests.")
    # Zero transport retries avoids accidentally resending a write after a lost
    # response. The SDK retains its own existing HTTP-status retry policy.
    session.mount("https://", TimeoutAdapter(max_retries=0))
    return client


def _order_status(remote_order) -> str:
    value = getattr(remote_order, "status", "")
    return str(getattr(value, "value", value)).lower()


def confirmed_order_fill(remote_order, fallback_date: date | None = None, reason: str = "",
                         default_side: Side | None = None) -> Fill | None:
    """Return the broker's cumulative execution, never an estimated fill."""
    quantity = float(getattr(remote_order, "filled_qty", 0) or 0)
    price = float(getattr(remote_order, "filled_avg_price", 0) or 0)
    if not math.isfinite(quantity) or quantity < 0:
        raise RuntimeError("Broker reported an unsupported fill quantity.")
    if quantity == 0:
        return None
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError("Broker reported an execution without a valid fill price.")
    side_value = getattr(remote_order, "side", default_side)
    side = Side(getattr(side_value, "value", side_value))
    if side is Side.HOLD:
        raise RuntimeError("Broker execution must have a buy or sell side.")
    timestamp = (getattr(remote_order, "filled_at", None) or getattr(remote_order, "updated_at", None)
                 or getattr(remote_order, "submitted_at", None))
    fill_date = timestamp.astimezone(MARKET_TZ).date() if timestamp else fallback_date or datetime.now(MARKET_TZ).date()
    confirmed_quantity = int(quantity) if quantity.is_integer() else quantity
    return Fill(fill_date, str(remote_order.symbol).upper(), side, confirmed_quantity, price,
                quantity * price, reason or f"confirmed Alpaca execution: {getattr(remote_order, 'client_order_id', '')}")


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
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.requests import GetCalendarRequest
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.requests import ReplaceOrderRequest
        from alpaca.trading.requests import StopOrderRequest
    except ImportError as exc:
        raise RuntimeError(
            "Alpaca support requires the official SDK. Install it with: pip install alpaca-py"
        ) from exc

    return {
        "StockHistoricalDataClient": StockHistoricalDataClient,
        "StockBarsRequest": StockBarsRequest,
        "StockLatestQuoteRequest": StockLatestQuoteRequest,
        "TimeFrame": TimeFrame,
        "TradingClient": TradingClient,
        "OrderSide": OrderSide,
        "OrderType": OrderType,
        "QueryOrderStatus": QueryOrderStatus,
        "TimeInForce": TimeInForce,
        "GetOrdersRequest": GetOrdersRequest,
        "GetCalendarRequest": GetCalendarRequest,
        "MarketOrderRequest": MarketOrderRequest,
        "LimitOrderRequest": LimitOrderRequest,
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
    normalized = base_url.rstrip("/").removesuffix("/v2")
    parsed = urlsplit(normalized)
    if (parsed.scheme != "https" or parsed.netloc != "paper-api.alpaca.markets"
            or parsed.path or parsed.query or parsed.fragment):
        raise RuntimeError("ALPACA_BASE_URL must be https://paper-api.alpaca.markets for paper trading.")
    return normalized


def _alpaca_error(exc: Exception) -> RuntimeError:
    return RuntimeError(f"Alpaca API request failed: {exc}")


class AlpacaMarketDataProvider:
    def __init__(self, days: int = 180, feed: str = "iex", exclude_date: date | None = None,
                 *, adjustment: str = "split") -> None:
        """Load split-adjusted signal history; other adjustments are explicit.

        Raw prices around a split can look like a large momentum move. Split
        adjustment keeps prior bars on a comparable share basis without treating
        dividend-adjusted prices as historical execution prices.
        """
        if adjustment not in ("raw", "split", "dividend", "all"):
            raise ValueError("adjustment must be raw, split, dividend, or all.")
        modules = _load_alpaca_modules()
        api_key, secret_key = _alpaca_keys()
        self.days = days
        self.feed = feed
        self.exclude_date = exclude_date
        self.adjustment = adjustment
        self._request_cls = modules["StockBarsRequest"]
        self._latest_quote_request = modules["StockLatestQuoteRequest"]
        self._timeframe = modules["TimeFrame"]
        self._client = _configure_request_timeouts(
            modules["StockHistoricalDataClient"](api_key, secret_key)
        )

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
            adjustment=self.adjustment,
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

    def latest_entry_prices(self, symbols: list[str]) -> dict[str, float]:
        """Size entries against a fresh executable ask, not yesterday's close."""
        normalized = sorted({symbol.upper() for symbol in symbols})
        if not normalized:
            return {}
        request = self._latest_quote_request(symbol_or_symbols=normalized, feed=self.feed)
        try:
            quotes = self._client.get_stock_latest_quote(request)
        except Exception as exc:
            raise RuntimeError(f"Cannot load fresh entry quotes from Alpaca ({self.feed} feed): {exc}") from exc
        now = datetime.now(timezone.utc)
        prices = {}
        for symbol in normalized:
            quote = quotes.get(symbol)
            if quote is None:
                raise RuntimeError(f"Missing latest quote for {symbol}; check feed access before entering trades.")
            timestamp = getattr(quote, "timestamp", None)
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise RuntimeError(f"Quote for {symbol} has no timezone-aware timestamp; refusing entry sizing.")
            age = (now - timestamp.astimezone(timezone.utc)).total_seconds()
            if not 0 <= age <= 120:
                raise RuntimeError(f"Stale or future quote for {symbol} (age {age:.1f}s; required 0–120s); wait for fresh market data.")
            try:
                bid = float(quote.bid_price)
                ask = float(quote.ask_price)
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Quote for {symbol} is missing valid bid/ask prices; refusing entry sizing.") from exc
            if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0 or bid > ask:
                raise RuntimeError(f"Invalid or crossed bid/ask quote for {symbol}; refusing entry sizing.")
            spread = (ask - bid) / ((ask + bid) / 2)
            if spread > 0.02:
                raise RuntimeError(f"Quote spread for {symbol} is {spread:.2%}, above the 2% entry limit.")
            prices[symbol] = ask
        return prices


class AlpacaPaperBroker:
    def __init__(self, *, entry_limit_bps: float | None = None) -> None:
        self._configure_entry_policy(entry_limit_bps)
        modules = _load_alpaca_modules()
        api_key, secret_key = _alpaca_keys()
        if not _paper_enabled():
            raise RuntimeError("This adapter only supports Alpaca paper trading. Set ALPACA_PAPER=true.")

        self._order_side = modules["OrderSide"]
        self._order_type = modules["OrderType"]
        self._query_order_status = modules["QueryOrderStatus"]
        self._time_in_force = modules["TimeInForce"]
        self._get_orders_request = modules["GetOrdersRequest"]
        self._get_calendar_request = modules["GetCalendarRequest"]
        self._market_order_request = modules["MarketOrderRequest"]
        self._limit_order_request = modules["LimitOrderRequest"]
        self._replace_order_request = modules["ReplaceOrderRequest"]
        self._stop_order_request = modules["StopOrderRequest"]
        self._client = _configure_request_timeouts(modules["TradingClient"](
            api_key,
            secret_key,
            paper=True,
            url_override=_trading_url_override(),
        ))
        self.cash = 0.0
        self.positions: dict[str, Position] = {}
        self.position_prices: dict[str, float] = {}
        self.fills: list[Fill] = []
        self.account_id: str | None = None
        self.last_submitted_order = None
        self.refresh()

    def refresh(self) -> None:
        try:
            account = self._client.get_account()
        except Exception as exc:
            raise _alpaca_error(exc)
        account_id = str(getattr(account, "id", "") or "") or None
        cash = float(account.cash)
        positions = {}
        position_prices = {}

        try:
            remote_positions = self._client.get_all_positions()
        except Exception as exc:
            raise _alpaca_error(exc)

        for remote_position in remote_positions:
            raw_quantity = float(remote_position.qty)
            if not math.isfinite(raw_quantity):
                raise RuntimeError("Broker returned an invalid position quantity.")
            quantity = int(raw_quantity) if raw_quantity.is_integer() else raw_quantity
            if quantity == 0:
                continue
            symbol = remote_position.symbol.upper()
            positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                average_cost=float(remote_position.avg_entry_price),
                broker_quantity=str(remote_position.qty),
            )
            position_prices[symbol] = self._remote_position_price(remote_position, quantity)

        # A failed positions request or malformed row must not leave a partially
        # refreshed view (for example new cash paired with cleared holdings).
        self.account = account
        self.account_id = account_id
        self.cash = cash
        self.positions = positions
        self.position_prices = position_prices

    def _remote_position_price(self, remote_position, quantity: int) -> float:
        raw_price = getattr(remote_position, "current_price", None)
        if raw_price not in (None, ""):
            return float(raw_price)
        market_value = float(getattr(remote_position, "market_value", 0.0) or 0.0)
        return market_value / quantity if quantity else 0.0

    def _configure_entry_policy(self, entry_limit_bps: float | None) -> None:
        if entry_limit_bps is not None and (
            isinstance(entry_limit_bps, bool)
            or not isinstance(entry_limit_bps, (float, int))
            or not math.isfinite(entry_limit_bps)
            or not 0 <= entry_limit_bps <= 100
        ):
            raise ValueError("Entry price allowance must be a finite number from 0 to 100 basis points.")
        self.entry_limit_bps = entry_limit_bps

    def maximum_entry_price(self, ask: float) -> float:
        """Return the highest permitted fill price for sizing an entry.

        Orders retain the fresh ask as their reference price. Sizing uses this
        capped price, and submit constructs the same cap once. IOC leaves no
        resting DAY entry to execute after its original signal has gone stale.
        """
        if isinstance(ask, bool) or not isinstance(ask, (float, int)) or not math.isfinite(ask) or ask <= 0:
            raise ValueError("Entry ask must be finite and positive.")
        allowance = getattr(self, "entry_limit_bps", None)
        if allowance is None:
            return ask
        with localcontext() as context:
            context.prec = 400
            cap = Decimal(str(ask)) * (Decimal(1) + Decimal(str(allowance)) / Decimal(10_000))
            tick = Decimal("0.01") if cap >= 1 else Decimal("0.0001")
            cap = cap.quantize(tick, rounding=ROUND_DOWN)
        result = float(cap)
        if not math.isfinite(result) or result <= 0:
            raise ValueError("Entry price cannot be represented within supported Alpaca price precision.")
        return result

    def _entry_request(self, order: Order):
        arguments = dict(symbol=order.symbol, qty=order.quantity,
                         side=self._order_side.BUY if order.side is Side.BUY else self._order_side.SELL,
                         client_order_id=order.client_order_id)
        if order.side is Side.BUY and getattr(self, "entry_limit_bps", None) is not None:
            return self._limit_order_request(
                **arguments, time_in_force=self._time_in_force.IOC,
                limit_price=self.maximum_entry_price(order.price), extended_hours=False,
            )
        return self._market_order_request(**arguments, time_in_force=self._time_in_force.DAY)

    def _validate_entry_execution(self, remote_order, order: Order) -> None:
        if order.side is not Side.BUY or getattr(self, "entry_limit_bps", None) is None:
            return
        kind = getattr(remote_order, "type", getattr(remote_order, "order_type", ""))
        force = getattr(remote_order, "time_in_force", "")
        try:
            limit = Decimal(str(getattr(remote_order, "limit_price", "")))
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError("Bounded entry response is missing a valid limit price.") from exc
        if (str(getattr(kind, "value", kind)).lower() != "limit"
                or str(getattr(force, "value", force)).lower() != "ioc"
                or not limit.is_finite() or limit <= 0
                or limit > Decimal(str(self.maximum_entry_price(order.price)))
                or getattr(remote_order, "extended_hours", False) is not False
                or str(getattr(remote_order, "client_order_id", "")) != order.client_order_id):
            raise RuntimeError("Broker entry does not match the bounded IOC limit policy; reconcile before trading.")
        try:
            filled_quantity = Decimal(str(getattr(remote_order, "filled_qty", 0) or 0))
            filled_price = Decimal(str(getattr(remote_order, "filled_avg_price", 0) or 0))
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError("Bounded entry response has an invalid execution.") from exc
        if (not filled_quantity.is_finite() or not 0 <= filled_quantity <= order.quantity
                or (filled_quantity and (not filled_price.is_finite() or not 0 < filled_price <= limit))):
            raise RuntimeError("Broker entry execution exceeds its submitted quantity or price limit; reconcile before trading.")

    def _validate_trading_request(self, operation: str, *, order: Order | None = None) -> None:
        """Hook for funded/read-only adapters, immediately before each write.

        The default paper adapter has no additional deployment authorization.
        A funded adapter must validate its account and authorization here,
        including mutations without an Order (replace/cancel).
        """

    def submit(self, order: Order, trade_date: date | None = None) -> Fill | None:
        if order.quantity <= 0 or order.side is Side.HOLD:
            return None

        if not order.client_order_id:
            raise ValueError("A stable client_order_id is required for broker submission.")
        if not math.isfinite(order.price) or order.price <= 0 or not float(order.quantity).is_integer():
            raise ValueError("Orders require a finite positive price and whole-share quantity.")
        self.last_submitted_order = None
        submitted_order = self.order_by_client_id(order.client_order_id)
        request = self._entry_request(order)
        if submitted_order is None:
            if order.side is Side.SELL:
                self.refresh()
                position = self.positions.get(order.symbol)
                if not position or position.quantity < order.quantity:
                    raise RuntimeError("Sell quantity exceeds the currently confirmed long position.")
            self._validate_trading_request("submit", order=order)
            try:
                submitted_order = self._client.submit_order(request)
            except Exception as exc:
                # A timeout can happen after the broker accepted an order. Recover
                # the same client ID; never retry with a different identifier.
                submitted_order = self.order_by_client_id(order.client_order_id)
                if submitted_order is None:
                    raise _alpaca_error(exc) from exc
        self._validate_order_identity(submitted_order, order)
        self._validate_entry_execution(submitted_order, order)
        if _order_status(submitted_order) not in TERMINAL_ORDER_STATUSES:
            submitted_order = self._poll_order(order.client_order_id, submitted_order)
            self._validate_order_identity(submitted_order, order)
            self._validate_entry_execution(submitted_order, order)
        self.last_submitted_order = submitted_order
        self.refresh()
        fill = confirmed_order_fill(submitted_order, trade_date, order.reason)
        if fill:
            self.fills.append(fill)
        return fill

    def order_by_client_id(self, client_order_id: str):
        try:
            return self._client.get_order_by_client_id(client_order_id)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "http_error", None), "response", None)
                status = getattr(status, "status_code", None)
            if status == 404:
                return None
            raise _alpaca_error(exc) from exc

    def _validate_order_identity(self, remote_order, order: Order) -> None:
        remote_side = str(getattr(getattr(remote_order, "side", ""), "value", getattr(remote_order, "side", ""))).lower()
        if (str(remote_order.symbol).upper() != order.symbol.upper() or remote_side != order.side.value
                or float(remote_order.qty) != order.quantity):
            raise RuntimeError("Existing client_order_id belongs to a different order; refusing to reuse it.")

    def _poll_order(self, client_order_id: str, remote_order):
        for _ in range(4):
            time.sleep(0.5)
            updated = self.order_by_client_id(client_order_id)
            if updated is not None:
                remote_order = updated
            if _order_status(remote_order) in TERMINAL_ORDER_STATUSES:
                break
        return remote_order

    def submit_protective_stop(self, symbol: str, quantity: int, stop_price: float, client_order_id: str):
        if quantity <= 0 or stop_price <= 0:
            return None
        if not math.isfinite(stop_price) or not math.isfinite(quantity) or not float(quantity).is_integer():
            raise ValueError("Protective orders require a finite positive stop price and whole-share quantity.")
        existing = self.order_by_client_id(client_order_id)
        if existing is not None:
            if _order_status(existing) in TERMINAL_ORDER_STATUSES:
                raise RuntimeError("Protective client_order_id already belongs to a completed order.")
            self._validate_order_identity(existing, Order(symbol, Side.SELL, quantity, stop_price, "protective stop", client_order_id))
            return existing
        request = self._stop_order_request(
            symbol=symbol,
            qty=quantity,
            side=self._order_side.SELL,
            type=self._order_type.STOP,
            time_in_force=self._time_in_force.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        self._validate_trading_request(
            "protect", order=Order(symbol, Side.SELL, quantity, stop_price, "protective stop", client_order_id),
        )
        try:
            return self._client.submit_order(request)
        except Exception as exc:
            existing = self.order_by_client_id(client_order_id)
            if existing is not None:
                return existing
            raise _alpaca_error(exc) from exc

    def replace_stop_order(self, order_id, stop_price: float, quantity: int | None = None):
        request = self._replace_order_request(
            qty=quantity,
            stop_price=round(stop_price, 2),
        )
        self._validate_trading_request("replace")
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
            self._validate_trading_request("cancel")
            try:
                self._client.cancel_order_by_id(order.id)
            except Exception as exc:
                raise _alpaca_error(exc)
            canceled += 1
        # Cancellation is asynchronous. An unconfirmed cancel still reserves
        # shares and may fill; submitting another sell could create a short.
        for _ in range(10):
            remaining = [order for order in self.open_orders([symbol])
                         if str(getattr(getattr(order, "side", ""), "value", getattr(order, "side", ""))).lower() == "sell"]
            if not remaining:
                self.refresh()
                return canceled
            time.sleep(0.2)
        raise RuntimeError(f"Sell cancellation for {symbol} is not confirmed; refusing a replacement exit.")

    def cancel_open_buy_orders(self, intent_client_ids: set[str]) -> list:
        """Cancel bot-owned entries and return their confirmed terminal state.

        A successful cancellation request is not confirmation: an entry can
        still execute during the race. Keep sell protection and foreign orders
        untouched, and require explicit order status before replanning exits.
        """
        from .reconcile import _owned_order_clients

        open_orders = list(self.open_orders())
        history = list(self.closed_orders())
        observed_ids = {str(getattr(remote, "client_order_id", "")) for remote in open_orders}
        resolved_intents = []
        for client_id in intent_client_ids - observed_ids:
            remote = self.order_by_client_id(client_id)
            if remote is None:
                raise RuntimeError("Pending entry is absent from broker order lookup; reconciliation required before liquidation.")
            side = getattr(remote, "side", "")
            if str(getattr(side, "value", side)).lower() != Side.BUY.value:
                raise RuntimeError("Pending entry lookup changed side; reconciliation required before liquidation.")
            if _order_status(remote) in TERMINAL_ORDER_STATUSES:
                resolved_intents.append(remote)
                history.append(remote)
            else:
                open_orders.append(remote)
        owned_ids, _ = _owned_order_clients([*history, *open_orders], set(), intent_client_ids)

        def owned_buy(remote) -> bool:
            side = getattr(remote, "side", "")
            return (str(getattr(side, "value", side)).lower() == Side.BUY.value
                    and str(getattr(remote, "client_order_id", "")) in owned_ids
                    and _order_status(remote) not in TERMINAL_ORDER_STATUSES)

        targets = [remote for remote in open_orders if owned_buy(remote)]
        for remote in targets:
            self._validate_trading_request("cancel")
            try:
                self._client.cancel_order_by_id(remote.id)
            except Exception:
                # A fill or a lost cancellation response can race this request.
                # Only the subsequent authoritative status resolves that race.
                pass

        confirmed = {}
        for _ in range(10):
            for remote in targets:
                client_id = str(remote.client_order_id)
                if client_id in confirmed:
                    continue
                current = self.order_by_client_id(client_id)
                if current is None:
                    continue
                side = getattr(current, "side", "")
                if (str(current.id) != str(remote.id)
                        or str(current.symbol).upper() != str(remote.symbol).upper()
                        or str(getattr(side, "value", side)).lower() != Side.BUY.value):
                    raise RuntimeError("Entry cancellation returned a different order; reconciliation required.")
                status = _order_status(current)
                if status == "replaced":
                    raise RuntimeError("Entry was replaced during cancellation; reconcile its successor before liquidation.")
                if status in TERMINAL_ORDER_STATUSES:
                    confirmed[client_id] = current
            if len(confirmed) == len(targets):
                remaining = list(self.open_orders())
                owned_ids, _ = _owned_order_clients(
                    [*history, *open_orders, *confirmed.values(), *remaining], set(), intent_client_ids,
                )
                if not any(owned_buy(remote) for remote in remaining):
                    self.refresh()
                    return [*resolved_intents, *confirmed.values()]
            time.sleep(0.2)
        raise RuntimeError("Buy cancellation is not confirmed; refusing liquidation against unresolved entries.")

    def open_client_order_ids(self, symbols: list[str] | None = None) -> set[str]:
        return {
            str(order.client_order_id)
            for order in self.open_orders(symbols)
            if getattr(order, "client_order_id", None)
        }

    def open_orders(self, symbols: list[str] | None = None):
        request = self._get_orders_request(
            status=self._query_order_status.OPEN,
            symbols=symbols,
            limit=500,
        )
        try:
            orders = self._client.get_orders(request)
            if len(orders) >= 500:
                raise RuntimeError("Open order list reached the API limit; refusing an incomplete risk view.")
            return orders
        except Exception as exc:
            raise _alpaca_error(exc)

    def closed_orders(self, symbols: list[str] | None = None):
        orders = []
        until = None
        while True:
            request = self._get_orders_request(
                status=self._query_order_status.CLOSED, symbols=symbols,
                limit=500, direction="desc", until=until,
            )
            try:
                page = self._client.get_orders(request)
            except Exception as exc:
                raise _alpaca_error(exc) from exc
            orders.extend(page)
            if len(page) < 500:
                return orders
            oldest = min(order.submitted_at for order in page)
            if until is not None and oldest >= until:
                raise RuntimeError("Order history pagination did not advance; refusing incomplete reconciliation.")
            # Include the boundary and deduplicate when synchronizing, so orders
            # sharing a submission timestamp are not silently lost.
            until = oldest

    def market_clock(self):
        try:
            return self._client.get_clock()
        except Exception as exc:
            raise _alpaca_error(exc)

    def trading_date(self) -> date:
        clock = self.market_clock()
        return clock.timestamp.astimezone(MARKET_TZ).date()

    def latest_completed_session(self) -> date:
        now = self.market_clock().timestamp.astimezone(MARKET_TZ)
        request = self._get_calendar_request(start=now.date() - timedelta(days=14), end=now.date())
        try:
            sessions = self._client.get_calendar(request)
        except Exception as exc:
            raise _alpaca_error(exc) from exc
        completed = []
        for session in sessions:
            close = session.close
            if close.tzinfo is None:
                close = close.replace(tzinfo=MARKET_TZ)
            if close <= now:
                completed.append(session.date)
        if not completed:
            raise RuntimeError("Cannot determine the most recently completed trading session.")
        return max(completed)

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
