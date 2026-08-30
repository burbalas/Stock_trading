from __future__ import annotations

import csv
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from .models import Fill, Order, Signal
from .time_utils import utc_to_local_iso


class TradeJournal:
    def __init__(self, directory: str | Path = "var") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.decisions_path = self.directory / "decisions.csv"
        self.fills_path = self.directory / "fills.csv"
        self.state_path = self.directory / "bot_state.json"

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

    def mark_submitted(self, key: str) -> None:
        state = self._read_state()
        keys = set(state.get("submitted_order_keys", []))
        keys.add(key)
        state["submitted_order_keys"] = sorted(keys)
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

    def expected_positions_from_fills(self) -> dict[str, int]:
        if not self.fills_path.exists():
            return {}

        submitted = set(self._read_state().get("submitted_order_keys", []))
        positions: dict[str, int] = {}
        with self.fills_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                key = f"{row.get('date')}:{row.get('symbol')}:{row.get('side')}"
                if key not in submitted:
                    continue
                symbol = str(row.get("symbol", "")).upper()
                side = str(row.get("side", "")).lower()
                quantity = int(float(row.get("quantity") or 0))
                if side == "buy":
                    positions[symbol] = positions.get(symbol, 0) + quantity
                elif side == "sell":
                    positions[symbol] = positions.get(symbol, 0) - quantity

        return {symbol: quantity for symbol, quantity in positions.items() if quantity > 0}

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

    def update_peak_equity(self, equity: float) -> tuple[float, float]:
        state = self._read_state()
        peak = max(float(state.get("peak_equity", 0.0)), equity)
        state["peak_equity"] = peak
        self._write_state(state)
        drawdown = (peak - equity) / peak if peak else 0.0
        return peak, drawdown

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

    def clear_protective_order(self, symbol: str) -> None:
        state = self._read_state()
        protective_orders = state.get("protective_orders", {})
        protective_orders.pop(symbol.upper(), None)
        state["protective_orders"] = dict(sorted(protective_orders.items()))
        self._write_state(state)

    def has_synced_order(self, client_order_id: str) -> bool:
        state = self._read_state()
        return client_order_id in state.get("synced_order_ids", [])

    def mark_synced_order(self, client_order_id: str) -> None:
        state = self._read_state()
        order_ids = set(state.get("synced_order_ids", []))
        order_ids.add(client_order_id)
        state["synced_order_ids"] = sorted(order_ids)
        self._write_state(state)

    def bind_account(self, account_id: str | None) -> None:
        if not account_id:
            return
        state = self._read_state()
        if state.get("account_id") == account_id:
            return
        archived = {
            "previous_account_id": state.get("account_id"),
            "previous_peak_equity": state.get("peak_equity"),
            "previous_submitted_order_keys": state.get("submitted_order_keys", []),
        }
        self._write_state(
            {
                "account_id": account_id,
                "peak_equity": 0.0,
                "submitted_order_keys": [],
                "archived_previous_state": archived,
            }
        )

    def reset_for_account(self, account_id: str | None, peak_equity: float = 0.0) -> Path:
        archive_dir = self.directory / "archive" / _now().replace(":", "-")
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

    def _append_csv(self, path: Path, headers: list[str], row: dict[str, Any]) -> None:
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        for attempt in range(5):
            try:
                raw = self.state_path.read_text(encoding="utf-8")
                return json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
        return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        tmp_path = self.state_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.state_path)


def _now() -> str:
    return utc_to_local_iso()
