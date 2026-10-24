from __future__ import annotations

import csv
import json
import math
import os
import time
import threading
from contextlib import contextmanager
from functools import wraps
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import Fill, Order, Signal
from .atomic_storage import cleanup_temporary, replace_file
from .runtime_paths import resolve_journal_directory
from .time_utils import utc_to_local_iso


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_DEPTH = threading.local()
_RISK_EXIT_REASONS = {"stop loss", "trailing stop", "account drawdown stop"}
_RETRYABLE_EXIT_STATUSES = {"canceled", "expired", "rejected"}
_MAX_EXIT_ATTEMPTS = 3


def _execution_decimal(value, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Invalid {label}; manual execution-ledger reconciliation required.") from exc
    if not number.is_finite() or number <= 0:
        raise RuntimeError(f"Invalid {label}; manual execution-ledger reconciliation required.")
    return number


def _validated_broker_history(rows: list[dict]) -> dict[str, tuple[tuple[str, str, str], Decimal, Decimal]]:
    """Validate each persisted cumulative cursor against its committed deltas."""
    cursors: dict[str, tuple[tuple[str, str, str], Decimal, Decimal]] = {}
    clients: dict[str, str] = {}
    for row in rows:
        broker_id = row.get("broker_order_id")
        if not broker_id:
            continue
        client_id = str(row.get("client_order_id") or "")
        identity = (str(row.get("symbol") or ""), str(row.get("side") or ""), client_id)
        if not identity[0] or identity[1] not in ("buy", "sell") or not client_id:
            raise RuntimeError("Invalid broker execution identity; manual reconciliation required.")
        if client_id in clients and clients[client_id] != broker_id:
            raise RuntimeError("Broker order identity changed for a client ID; manual reconciliation required.")
        clients[client_id] = broker_id
        quantity = _execution_decimal(row.get("quantity"), "journal execution quantity")
        value = _execution_decimal(row.get("value"), "journal execution value")
        cumulative_qty = _execution_decimal(row.get("broker_filled_quantity"), "broker quantity cursor")
        cumulative_value = _execution_decimal(row.get("broker_filled_value"), "broker value cursor")
        previous_identity, previous_qty, previous_value = cursors.get(broker_id, (identity, Decimal(0), Decimal(0)))
        if (previous_identity != identity or cumulative_qty != previous_qty + quantity
                or abs(cumulative_value - previous_value - value) > Decimal("0.00000001")):
            raise RuntimeError("Broker execution identity or cumulative cursor disagrees with committed fills; "
                               "manual reconciliation required.")
        cursors[broker_id] = (identity, cumulative_qty, cumulative_value)
    return cursors


def _locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock():
            return method(self, *args, **kwargs)
    return wrapped


class TradeJournal:
    def __init__(self, directory: str | Path = "var") -> None:
        self.directory = resolve_journal_directory(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.decisions_path = self.directory / "decisions.csv"
        self.fills_path = self.directory / "fills.csv"
        self.state_path = self.directory / "bot_state.json"

    @contextmanager
    def lock(self, name: str = "state", timeout: float = 10.0):
        """An OS-held lock, released even when the process crashes."""
        if not name.replace("-", "").isalnum():
            raise ValueError("Invalid journal lock name")
        path = self.directory / f".{name}.lock"
        key = str(path.resolve())
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        if not thread_lock.acquire(timeout=max(0.0, timeout)):
            raise RuntimeError(f"Another process is using journal lock '{name}'.")
        depths = getattr(_LOCK_DEPTH, "depths", None)
        if depths is None:
            depths = _LOCK_DEPTH.depths = {}
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
                thread_lock.release()
            return
        handle = None
        acquired = False
        try:
            handle = path.open("a+b")
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Another process is using journal lock '{name}'.") from exc
                    time.sleep(0.05)
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            if handle is not None:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            thread_lock.release()

    def record_decision(
        self,
        signal: Signal,
        order: Order | None,
        equity: float,
        mode: str,
        rejected_reason: str | None = None,
    ) -> None:
        self._append_csv(
            self.decisions_path,
            [
                "timestamp",
                "mode",
                "symbol",
                "signal_side",
                "confidence",
                "order_side",
                "quantity",
                "price",
                "equity",
                "reason",
                "rejected_reason",
            ],
            {
                "timestamp": _now(),
                "mode": mode,
                "symbol": signal.symbol,
                "signal_side": signal.side.value,
                "confidence": f"{signal.confidence:.4f}",
                "order_side": order.side.value if order else "",
                "quantity": order.quantity if order else "",
                "price": f"{order.price:.4f}" if order else "",
                "equity": f"{equity:.2f}",
                "reason": signal.reason,
                "rejected_reason": rejected_reason or "",
            },
        )

    def record_fill(self, fill: Fill, mode: str) -> None:
        self._append_csv(
            self.fills_path,
            ["timestamp", "mode", "date", "symbol", "side", "quantity", "price", "value", "reason"],
            {
                "timestamp": _now(),
                "mode": mode,
                "date": fill.date.isoformat(),
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": f"{fill.price:.4f}",
                "value": f"{fill.value:.2f}",
                "reason": fill.reason,
            },
        )

    def has_submitted(self, key: str) -> bool:
        state = self._read_state()
        return key in state.get("submitted_order_keys", [])

    @_locked
    def mark_submitted(self, key: str) -> None:
        state = self._read_state()
        keys = set(state.get("submitted_order_keys", []))
        keys.add(key)
        state["submitted_order_keys"] = sorted(keys)
        self._write_state(state)

    @_locked
    def reserve_order(self, order: Order, trade_date: date | None, max_daily_buys: int | None = None) -> bool:
        """Persist intent before network I/O, including ambiguous submit outcomes."""
        key = self.order_key(order, trade_date)
        state = self._read_state()
        if order.side.value == "buy" and self._drawdown_halted(state):
            return False
        keys = set(state.get("submitted_order_keys", []))
        client_id = order.client_order_id or self.client_order_id(order, trade_date)
        pending = state.setdefault("order_intents", {})
        if client_id in pending:
            return False
        authorization = state.get("risk_exit_retry_authorizations", {}).get(client_id)
        retry_id = client_id.startswith(self.client_order_id(order, trade_date) + "-retry-")
        if authorization is not None:
            previous_id = authorization.get("previous_client_order_id")
            previous = pending.get(previous_id, {})
            if (order.side.value != "sell" or order.reason not in _RISK_EXIT_REASONS
                    or authorization.get("key") != key or client_id in pending
                    or not 0 < order.quantity <= authorization.get("quantity", 0)
                    or previous.get("status") not in _RETRYABLE_EXIT_STATUSES
                    or self._latest_exit_attempt(state, order, trade_date) != previous_id):
                return False
        elif key in keys or retry_id:
            return False
        if (order.side.value == "buy" and max_daily_buys is not None
                and self.submitted_count_for_date(trade_date, "buy") >= max_daily_buys):
            return False
        keys.add(key)
        state["submitted_order_keys"] = sorted(keys)
        pending[client_id] = {
            "key": key, "symbol": order.symbol, "side": order.side.value,
            "quantity": order.quantity, "reason": order.reason,
        }
        if authorization is not None:
            pending[client_id]["retry_of"] = authorization["previous_client_order_id"]
            state["risk_exit_retry_authorizations"].pop(client_id)
        self._write_state(state)
        return True

    @classmethod
    def _latest_exit_attempt(cls, state, order: Order, trade_date: date | None) -> str | None:
        base_id = cls.client_order_id(order, trade_date)
        intents = state.get("order_intents", {})
        if base_id not in intents:
            return None
        latest = base_id
        for attempt in range(2, _MAX_EXIT_ATTEMPTS + 1):
            candidate = f"{base_id}-retry-{attempt}"
            if candidate not in intents:
                break
            if intents[candidate].get("retry_of") != latest:
                raise RuntimeError("Risk exit retry history is inconsistent; reconcile before retrying.")
            latest = candidate
        return latest

    def latest_risk_exit_client_id(self, order: Order, trade_date: date | None) -> str | None:
        """Identify the reserved attempt that must be looked up directly at the broker."""
        if order.side.value != "sell" or order.reason not in _RISK_EXIT_REASONS:
            return None
        return self._latest_exit_attempt(self._read_state(), order, trade_date)

    @_locked
    def prepare_risk_exit_retry(self, order: Order, trade_date: date | None, remote_order,
                               max_attempts: int = _MAX_EXIT_ATTEMPTS) -> Order | None:
        """Authorize a bounded retry using a freshly fetched terminal broker order.

        The caller must refresh holdings and cap ``order`` to the currently
        confirmed long position. Unknown, active, replaced and fully executed
        orders never authorize a new attempt. Preparing does not reserve or
        submit the retry; ``reserve_order`` commits its unique intent atomically.
        """
        from .alpaca_integration import _order_status

        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or not 1 <= max_attempts <= _MAX_EXIT_ATTEMPTS):
            raise ValueError("Risk exits allow between one and three attempts per day.")
        if order.side.value != "sell" or order.reason not in _RISK_EXIT_REASONS or remote_order is None:
            return None
        state = self._read_state()
        previous_id = self._latest_exit_attempt(state, order, trade_date)
        if previous_id is None:
            return None
        base_id = self.client_order_id(order, trade_date)
        attempt = 1 if previous_id == base_id else int(previous_id.rsplit("-", 1)[1])
        if attempt >= max_attempts:
            return None
        previous = state["order_intents"][previous_id]
        side = getattr(remote_order, "side", "")
        try:
            quantity = float(getattr(remote_order, "qty", 0))
            filled = float(getattr(remote_order, "filled_qty", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Risk exit retry received invalid broker quantities.") from exc
        if (str(getattr(remote_order, "client_order_id", "")) != previous_id
                or str(getattr(remote_order, "symbol", "")).upper() != order.symbol
                or str(getattr(side, "value", side)).lower() != "sell"
                or previous.get("side") != "sell" or previous.get("symbol") != order.symbol
                or previous.get("key") != self.order_key(order, trade_date)
                or not math.isfinite(quantity) or quantity != previous.get("quantity")
                or not math.isfinite(filled) or not 0 <= filled <= quantity):
            raise RuntimeError("Risk exit retry broker identity does not match its reserved intent.")
        status = _order_status(remote_order)
        if status not in _RETRYABLE_EXIT_STATUSES:
            return None
        remaining = Decimal(str(quantity)) - Decimal(str(filled))
        if remaining <= 0 or remaining != remaining.to_integral_value():
            return None
        if self.fills_path.exists():
            with self.fills_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("client_order_id") != previous_id:
                        continue
                    if (row.get("broker_order_id") != str(getattr(remote_order, "id", ""))
                            or float(row.get("broker_filled_quantity") or 0) > filled):
                        raise RuntimeError("Risk exit retry execution identity or quantity regressed; reconcile before retrying.")
        # Commit a raced partial fill under its original broker ID before
        # authorizing a new order. A corrected/regressed fill fails closed here.
        self.record_broker_order(remote_order)
        state = self._read_state()
        state["order_intents"][previous_id]["status"] = status
        retry = replace(order, quantity=min(order.quantity, int(remaining)),
                        client_order_id=f"{base_id}-retry-{attempt + 1}")
        state.setdefault("risk_exit_retry_authorizations", {})[retry.client_order_id] = {
            "key": self.order_key(order, trade_date),
            "previous_client_order_id": previous_id,
            "quantity": retry.quantity,
        }
        self._write_state(state)
        return retry

    @_locked
    def record_broker_order(self, remote_order, mode: str = "alpaca-paper-sync") -> Fill | None:
        """Atomically journal only the new quantity of a cumulative broker fill.

        The CSV stores the cursor alongside the fill, so a crash cannot commit a
        fill without its deduplication cursor (or a cursor without the fill).
        """
        from .alpaca_integration import confirmed_order_fill
        from .models import Side

        client_id = str(getattr(remote_order, "client_order_id", "") or "")
        broker_id = str(getattr(remote_order, "id", "") or client_id)
        if not broker_id or not client_id:
            raise ValueError("Broker order and client IDs are required to synchronize executions.")
        state = self._read_state()
        protective_ids = {details.get("client_order_id") for details in state.get("protective_orders", {}).values()}
        protective = client_id.startswith("bot-protect-") or client_id in protective_ids
        fill = confirmed_order_fill(remote_order, default_side=Side.SELL if protective else None)
        if fill is None:
            return None
        if not fill.symbol or fill.symbol != fill.symbol.strip():
            raise RuntimeError("Broker execution symbol is invalid; manual reconciliation required.")
        cumulative_qty = _execution_decimal(getattr(remote_order, "filled_qty", None), "broker fill quantity")
        cumulative_price = _execution_decimal(getattr(remote_order, "filled_avg_price", None), "broker fill price")
        cumulative_value = cumulative_qty * cumulative_price
        requested_qty = getattr(remote_order, "qty", None)
        if requested_qty is not None and cumulative_qty > _execution_decimal(requested_qty, "broker order quantity"):
            raise RuntimeError("Broker fill quantity exceeds its order quantity; manual reconciliation required.")
        rows = []
        headers = ["timestamp", "mode", "date", "symbol", "side", "quantity", "price", "value", "reason"]
        if self.fills_path.exists():
            with self.fills_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                headers = list(reader.fieldnames or headers)
                rows = list(reader)
        for header in ("broker_order_id", "client_order_id", "broker_filled_quantity", "broker_filled_value"):
            if header not in headers:
                headers.append(header)
        cursors = _validated_broker_history(rows)
        identity = (fill.symbol, fill.side.value, client_id)
        previous_identity, previous_qty, previous_value = cursors.get(broker_id, (identity, Decimal(0), Decimal(0)))
        if previous_identity != identity or any(
                row.get("client_order_id") == client_id and row.get("broker_order_id") not in (None, "", broker_id)
                for row in rows):
            raise RuntimeError("Broker execution identity changed; manual reconciliation required.")
        if cumulative_qty < previous_qty:
            raise RuntimeError(f"Broker fill quantity regressed for {client_id}; manual reconciliation required.")
        if cumulative_qty == previous_qty:
            if abs(cumulative_value - previous_value) > Decimal("0.00000001"):
                raise RuntimeError(f"Broker corrected an existing fill for {client_id}; manual reconciliation required.")
            return None
        # A legacy journal had one daily normal order per symbol/side and no
        # broker cursor. Adopt its existing confirmed row rather than doubling it.
        legacy = [row for row in rows if not row.get("broker_order_id")
                  and row.get("date") == fill.date.isoformat() and row.get("symbol") == fill.symbol
                  and row.get("side") == fill.side.value and str(row.get("mode", "")).startswith("alpaca")]
        if not previous_qty and legacy:
            if len(legacy) != 1 or _execution_decimal(legacy[0]["quantity"], "legacy execution quantity") != cumulative_qty:
                raise RuntimeError(f"Legacy fills disagree with broker order {client_id}; reconcile the journal before trading.")
            row = legacy[0]
            row.update(price=str(cumulative_price), value=str(cumulative_value), quantity=str(cumulative_qty),
                       broker_order_id=broker_id, client_order_id=client_id,
                       broker_filled_quantity=str(cumulative_qty), broker_filled_value=str(cumulative_value))
            self._replace_csv(self.fills_path, headers, rows)
            self.mark_submitted(f"{fill.date.isoformat()}:{fill.symbol}:{fill.side.value}")
            return None
        exact_quantity = cumulative_qty - previous_qty
        quantity = int(exact_quantity) if exact_quantity == exact_quantity.to_integral_value() else float(exact_quantity)
        exact_value = cumulative_value - previous_value
        value = float(exact_value)
        if not math.isfinite(value) or value <= 0 or not math.isfinite(quantity) or quantity <= 0:
            raise RuntimeError(f"Invalid cumulative execution value for {client_id}.")
        delta = Fill(fill.date, fill.symbol, fill.side, quantity, value / quantity, value, fill.reason)
        rows.append({"timestamp": _now(), "mode": mode, "date": delta.date.isoformat(), "symbol": delta.symbol,
                     "side": delta.side.value, "quantity": str(exact_quantity), "price": f"{delta.price:.10f}",
                     "value": str(exact_value), "reason": delta.reason, "broker_order_id": broker_id,
                     "client_order_id": client_id, "broker_filled_quantity": str(cumulative_qty),
                     "broker_filled_value": str(cumulative_value)})
        self._replace_csv(self.fills_path, headers, rows)
        self.mark_submitted(f"{fill.date.isoformat()}:{fill.symbol}:{fill.side.value}")
        return delta

    @_locked
    def update_order_status(self, client_order_id: str, status: str) -> None:
        state = self._read_state()
        intents = state.get("order_intents", {})
        if client_order_id in intents:
            intents[client_order_id]["status"] = status
            self._write_state(state)

    def submitted_count_for_date(self, trade_date: date | None, side: str | None = None) -> int:
        date_part = (trade_date or date.today()).isoformat()
        state = self._read_state()
        suffix = f":{side}" if side else ""
        return sum(
            1
            for key in state.get("submitted_order_keys", [])
            if str(key).startswith(f"{date_part}:") and (not suffix or str(key).endswith(suffix))
        )

    def expected_positions_from_fills(self) -> dict[str, int | float]:
        positions = self.expected_positions_decimal_from_fills()
        return {symbol: int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
                for symbol, quantity in positions.items()}

    @_locked
    def broker_execution_client_ids(self) -> set[str]:
        """Return clients with modern, validated execution cursors."""
        if not self.fills_path.exists():
            return set()
        with self.fills_path.open(newline="", encoding="utf-8") as handle:
            cursors = _validated_broker_history(list(csv.DictReader(handle)))
        return {identity[2] for identity, _quantity, _value in cursors.values()}

    @_locked
    def expected_positions_decimal_from_fills(self) -> dict[str, Decimal]:
        """Read the authoritative execution balance without float rounding."""
        if not self.fills_path.exists():
            return {}

        submitted = set(self._read_state().get("submitted_order_keys", []))
        positions: dict[str, Decimal] = {}
        with self.fills_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            _validated_broker_history(rows)
            for row in rows:
                key = f"{row.get('date')}:{row.get('symbol')}:{row.get('side')}"
                if key not in submitted and not row.get("broker_order_id"):
                    continue
                symbol = str(row.get("symbol", "")).upper()
                side = str(row.get("side", "")).lower()
                if not symbol or symbol != symbol.strip() or side not in ("buy", "sell"):
                    raise RuntimeError("Fill journal contains an invalid execution identity; reconcile it before trading.")
                try:
                    date.fromisoformat(str(row.get("date", "")))
                    quantity = Decimal(str(row.get("quantity") or "0"))
                except (ValueError, InvalidOperation) as exc:
                    raise RuntimeError("Fill journal contains an invalid execution; reconcile it before trading.") from exc
                if not quantity.is_finite() or quantity <= 0:
                    raise RuntimeError("Fill journal contains an invalid quantity; reconcile it before trading.")
                if side == "buy":
                    positions[symbol] = positions.get(symbol, Decimal(0)) + quantity
                elif side == "sell":
                    positions[symbol] = positions.get(symbol, Decimal(0)) - quantity

        return {symbol: quantity for symbol, quantity in positions.items() if quantity != 0}

    def recent_exit_dates(self) -> dict[str, date]:
        if not self.fills_path.exists():
            return {}

        exits: dict[str, date] = {}
        with self.fills_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("side", "")).lower() != "sell":
                    continue
                symbol = str(row.get("symbol", "")).upper()
                try:
                    exit_date = date.fromisoformat(str(row.get("date", "")))
                except ValueError:
                    continue
                if symbol and exit_date > exits.get(symbol, date.min):
                    exits[symbol] = exit_date
        return exits

    @_locked
    def update_peak_equity(self, equity: float) -> tuple[float, float]:
        state = self._read_state()
        peak, drawdown = self._equity_drawdown(state, equity)
        state["peak_equity"] = peak
        self._write_state(state)
        return peak, drawdown

    @_locked
    def update_drawdown_state(self, equity: float, stop_pct: float) -> tuple[float, float, bool]:
        """Persist a breached account circuit before planning or submitting orders.

        A rebound, a process restart, or a different threshold never clears a
        previously latched halt. Account resets remain explicit journal actions.
        """
        if (isinstance(stop_pct, bool) or not isinstance(stop_pct, (int, float))
                or not math.isfinite(stop_pct) or not 0 < stop_pct < 1):
            raise ValueError("Drawdown stop must be a finite fraction between zero and one.")
        state = self._read_state()
        peak, drawdown = self._equity_drawdown(state, equity)
        halted = self._drawdown_halted(state)
        if not halted and drawdown >= stop_pct:
            halted = True
            state["drawdown_halt"] = {
                "triggered_at": _now(), "equity": equity, "peak_equity": peak,
                "drawdown_pct": drawdown, "stop_pct": stop_pct,
            }
        state["peak_equity"] = peak
        state["drawdown_halted"] = halted
        self._write_state(state)
        return peak, drawdown, halted

    @_locked
    def latch_risk_halt(self, reason: str, metadata: dict | None = None) -> bool:
        """Persist an additional risk circuit without clearing an earlier one.

        The first halt record remains authoritative. Later distinct reasons
        remain visible in risk_halt_events, including absolute funded losses
        discovered after the percentage drawdown circuit already tripped.
        """
        if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
            raise ValueError("Risk halt reason must be a nonempty string without surrounding whitespace.")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("Risk halt metadata must be an object.")
        try:
            payload = json.loads(json.dumps(metadata or {}, allow_nan=False, sort_keys=True))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Risk halt metadata must contain finite JSON values.") from exc
        state = self._read_state()
        halted = self._drawdown_halted(state)
        events = state.get("risk_halt_events", [])
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise RuntimeError("Journal risk halt history is invalid; reconcile it before trading.")
        if not any(event.get("reason") == reason and event.get("metadata") == payload for event in events):
            event = {"reason": reason, "metadata": payload, "triggered_at": _now()}
            events.append(event)
            state["risk_halt_events"] = events
        else:
            event = next(event for event in events
                         if event.get("reason") == reason and event.get("metadata") == payload)
        if not halted or not state.get("drawdown_halt"):
            state["drawdown_halt"] = dict(event)
        state["drawdown_halted"] = True
        self._write_state(state)
        return True

    @staticmethod
    def _drawdown_halted(state: dict[str, Any]) -> bool:
        halted = state.get("drawdown_halted", False)
        if not isinstance(halted, bool):
            raise RuntimeError("Journal drawdown latch is invalid; restore or reconcile it before trading.")
        return halted

    @staticmethod
    def _equity_drawdown(state: dict[str, Any], equity: float) -> tuple[float, float]:
        previous_peak = state.get("peak_equity", 0.0)
        for name, value in (("equity", equity), ("peak equity", previous_peak)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"Journal {name} must be finite and nonnegative.")
        peak = max(previous_peak, equity)
        return peak, (peak - equity) / peak if peak else 0.0

    @_locked
    def update_position_highs(self, prices: dict[str, float], held_symbols: set[str]) -> dict[str, float]:
        state = self._read_state()
        current_highs = {
            str(symbol).upper(): float(price)
            for symbol, price in state.get("position_highs", {}).items()
            if str(symbol).upper() in held_symbols
        }
        for symbol in held_symbols:
            price = prices.get(symbol)
            if price is None:
                continue
            current_highs[symbol] = max(current_highs.get(symbol, 0.0), float(price))
        state["position_highs"] = dict(sorted(current_highs.items()))
        self._write_state(state)
        return current_highs

    @_locked
    def mark_protective_order(self, symbol: str, client_order_id: str, stop_price: float, quantity: int) -> None:
        state = self._read_state()
        protective_orders = state.get("protective_orders", {})
        protective_orders[symbol.upper()] = {
            "client_order_id": client_order_id,
            "stop_price": round(float(stop_price), 2),
            "quantity": int(quantity),
        }
        state["protective_orders"] = dict(sorted(protective_orders.items()))
        self._write_state(state)

    @_locked
    def clear_protective_order(self, symbol: str) -> None:
        state = self._read_state()
        protective_orders = state.get("protective_orders", {})
        protective_orders.pop(symbol.upper(), None)
        state["protective_orders"] = dict(sorted(protective_orders.items()))
        self._write_state(state)

    def has_synced_order(self, client_order_id: str) -> bool:
        state = self._read_state()
        return client_order_id in state.get("synced_order_ids", [])

    @_locked
    def mark_synced_order(self, client_order_id: str) -> None:
        state = self._read_state()
        order_ids = set(state.get("synced_order_ids", []))
        order_ids.add(client_order_id)
        state["synced_order_ids"] = sorted(order_ids)
        self._write_state(state)

    @_locked
    def bind_account(self, account_id: str | None) -> None:
        if not account_id:
            return
        state = self._read_state()
        if state.get("account_id") == account_id:
            return
        if state.get("account_id"):
            raise RuntimeError("Connected account differs from the journal account; reconcile or explicitly archive it before trading.")
        else:
            # First binding must not erase reservations/confirmed fills created
            # before an engine was instantiated.
            state["account_id"] = account_id
            self._write_state(state)

    @_locked
    def reset_for_account(self, account_id: str | None, peak_equity: float = 0.0) -> Path:
        archive_dir = self.directory / "archive" / f"{_now().replace(':', '-')}-{time.time_ns()}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.decisions_path, self.fills_path, self.state_path):
            if path.exists():
                path.rename(archive_dir / path.name)
        self._write_state(
            {
                "account_id": account_id,
                "peak_equity": peak_equity,
                "submitted_order_keys": [],
                "reset_archive": str(archive_dir),
            }
        )
        return archive_dir

    @staticmethod
    def order_key(order: Order, trade_date: date | None) -> str:
        date_part = (trade_date or date.today()).isoformat()
        return f"{date_part}:{order.symbol}:{order.side.value}"

    @staticmethod
    def client_order_id(order: Order, trade_date: date | None) -> str:
        return "bot-" + TradeJournal.order_key(order, trade_date).replace(":", "-").lower()

    @_locked
    def _append_csv(self, path: Path, headers: list[str], row: dict[str, Any]) -> None:
        exists = path.exists()
        if exists:
            with path.open(newline="", encoding="utf-8") as handle:
                headers = next(csv.reader(handle), headers)
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            if not exists:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())

    def _replace_csv(self, path: Path, headers: list[str], rows: list[dict]) -> None:
        tmp_path = path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with tmp_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(tmp_path, path)
        finally:
            cleanup_temporary(tmp_path)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        for attempt in range(5):
            try:
                raw = self.state_path.read_text(encoding="utf-8")
                if not raw.strip():
                    raise RuntimeError("Journal state is empty; restore or reconcile it before trading.")
                state = json.loads(raw)
                if not isinstance(state, dict):
                    raise RuntimeError("Journal state must be an object; restore or reconcile it before trading.")
                return state
            except json.JSONDecodeError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
        return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        tmp_path = self.state_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(tmp_path, self.state_path)
        finally:
            cleanup_temporary(tmp_path)


def _now() -> str:
    return utc_to_local_iso()
