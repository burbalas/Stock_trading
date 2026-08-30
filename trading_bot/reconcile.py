from __future__ import annotations

from dataclasses import dataclass

from .journal import TradeJournal


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    messages: list[str]


def reconcile_positions(journal: TradeJournal, broker) -> ReconciliationResult:
    expected = journal.expected_positions_from_fills()
    actual = {
        symbol: position.quantity
        for symbol, position in broker.positions.items()
        if position.quantity > 0
    }
    messages: list[str] = []

    for symbol, expected_qty in sorted(expected.items()):
        actual_qty = actual.get(symbol, 0)
        if actual_qty != expected_qty:
            messages.append(f"{symbol}: bot expected {expected_qty} shares, Alpaca shows {actual_qty}")

    for symbol, actual_qty in sorted(actual.items()):
        if symbol not in expected:
            messages.append(f"{symbol}: Alpaca shows {actual_qty} shares not recorded in bot fills")

    return ReconciliationResult(ok=not messages, messages=messages)
