from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os

from .alpaca_integration import TERMINAL_ORDER_STATUSES, _order_status
from .journal import TradeJournal
from .models import Side
from .time_utils import MARKET_TZ


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    symbol: str
    expected_quantity: str | None
    actual_quantity: str | None
    action: str


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    messages: list[str]
    issues: list[ReconciliationIssue] = field(default_factory=list)


def _owned_order_clients(remote_orders, protective_ids: set[str], intent_ids) -> tuple[set[str], set[str]]:
    """Recover ownership across broker-generated replacement IDs.

    Alpaca can assign a new client ID when replacing an order. Its broker-ID
    links preserve the chain even when no local protective mapping survives.
    Traverse in both directions to include executions on replaced ancestors.
    """
    orders = {
        str(getattr(order, "id", None) or getattr(order, "client_order_id", "")): order
        for order in remote_orders
    }
    orders.pop("", None)
    neighbors: dict[str, set[str]] = {order_id: set() for order_id in orders}
    owned_seeds: set[str] = set()
    protective_seeds: set[str] = set()
    for order_id, remote in orders.items():
        client_id = str(getattr(remote, "client_order_id", "") or "")
        if client_id.startswith("bot-protect-") or client_id in protective_ids:
            protective_seeds.add(order_id)
        if client_id.startswith("bot-") or client_id in protective_ids or client_id in intent_ids:
            owned_seeds.add(order_id)
        for attribute in ("replaces", "replaced_by"):
            related_id = str(getattr(remote, attribute, None) or "")
            if related_id in orders:
                neighbors[order_id].add(related_id)
                neighbors[related_id].add(order_id)

    def identity(order_id: str) -> tuple[str, str]:
        remote = orders[order_id]
        side = getattr(remote, "side", Side.SELL if order_id in protective_seeds else None)
        return str(getattr(remote, "symbol", "")).upper(), str(getattr(side, "value", side)).lower()

    def expand(seeds: set[str]) -> set[str]:
        visited = set(seeds)
        pending = list(seeds)
        while pending:
            order_id = pending.pop()
            for related_id in neighbors[order_id]:
                if identity(order_id) != identity(related_id):
                    raise RuntimeError("Broker replacement chain changed symbol or side; manual reconciliation required.")
                if related_id not in visited:
                    visited.add(related_id)
                    pending.append(related_id)
        return {str(getattr(orders[order_id], "client_order_id", "") or "") for order_id in visited}

    return expand(owned_seeds), expand(protective_seeds)


def sync_broker_orders(journal: TradeJournal, broker) -> int:
    account_id = getattr(broker, "account_id", None)
    expected = os.environ.get("STOCK_TRADING_EXPECTED_ACCOUNT_ID")
    if expected and account_id != expected:
        raise RuntimeError("Connected account differs from the managed paper account; refusing synchronization.")
    if isinstance(account_id, str) and account_id:
        journal.bind_account(account_id)
    """Recover confirmed fills for normal, partial, canceled and stop orders."""
    journal.bind_account(getattr(broker, "account_id", None))
    state = journal._read_state()
    protective_ids = {str(details.get("client_order_id", ""))
                      for details in state.get("protective_orders", {}).values()}
    intents = state.get("order_intents", {})
    remote_orders = list(broker.closed_orders()) if hasattr(broker, "closed_orders") else []
    open_orders = []
    if hasattr(broker, "open_orders"):
        open_orders = list(broker.open_orders())
        remote_orders.extend(open_orders)
    seen = {str(getattr(order, "client_order_id", "") or "") for order in remote_orders}
    if hasattr(broker, "order_by_client_id"):
        for client_id, details in intents.items():
            if client_id in seen or details.get("status") in TERMINAL_ORDER_STATUSES:
                continue
            remote = broker.order_by_client_id(client_id)
            if remote is not None:
                remote_orders.append(remote)
    owned_ids, protective_ids = _owned_order_clients(remote_orders, protective_ids, intents)
    by_id = {str(remote.id): remote for remote in remote_orders if getattr(remote, "id", None)}
    replaced_ancestors = {str(remote.replaces) for remote in remote_orders if getattr(remote, "replaces", None)}
    replaced_ancestors.update(
        str(getattr(remote, "id", "")) for remote in remote_orders
        if str(getattr(remote, "replaced_by", "")) in by_id
    )
    for order_id in replaced_ancestors:
        ancestor = by_id.get(order_id)
        if ancestor is not None and str(getattr(ancestor, "client_order_id", "")) in owned_ids:
            if float(getattr(ancestor, "filled_qty", 0) or 0) > 0:
                # A replacement may carry cumulative execution from its parent.
                # Per-order totals alone cannot establish whether it is inherited.
                raise RuntimeError(
                    "Partially filled replacement history requires execution-ledger reconciliation; "
                    "refusing to double-count possible inherited fills."
                )
    active_sell_symbols = {
        str(remote.symbol).upper() for remote in open_orders
        if _order_status(remote) not in TERMINAL_ORDER_STATUSES
        and str(getattr(getattr(remote, "side", ""), "value", getattr(remote, "side", ""))).lower() == Side.SELL.value
    }
    cursor_clients = journal.broker_execution_client_ids()
    synced = 0
    for remote in remote_orders:
        client_id = str(getattr(remote, "client_order_id", "") or "")
        if not client_id or client_id not in owned_ids:
            continue
        status = _order_status(remote)
        protective = client_id.startswith("bot-protect-") or client_id in protective_ids
        # Legacy protective fills were already committed without broker cursors.
        if protective and journal.has_synced_order(client_id) and client_id not in cursor_clients:
            continue
        mode = "alpaca-live-sync" if getattr(broker, "execution_mode", None) == "alpaca-live" else "alpaca-paper-sync"
        fill = journal.record_broker_order(remote, mode=mode)
        if fill:
            synced += 1
        submitted_at = getattr(remote, "submitted_at", None)
        if submitted_at is not None:
            side_value = getattr(remote, "side", Side.SELL if protective else None)
            side = Side(getattr(side_value, "value", side_value))
            order_date = submitted_at.astimezone(MARKET_TZ).date()
            journal.mark_submitted(f"{order_date.isoformat()}:{str(remote.symbol).upper()}:{side.value}")
        journal.update_order_status(client_id, status)
        if protective and status == "filled":
            journal.mark_synced_order(client_id)
            symbol = str(remote.symbol).upper()
            tracked = state.get("protective_orders", {}).get(symbol, {})
            if tracked.get("client_order_id") == client_id or symbol not in active_sell_symbols:
                journal.clear_protective_order(symbol)
    if hasattr(broker, "refresh"):
        broker.refresh()
    return synced


def reconcile_positions(journal: TradeJournal, broker) -> ReconciliationResult:
    """Compare confirmed executions without inventing corporate adjustments.

    Matching fractional/short holdings also block this whole-share, long-only
    operator: it cannot promise to liquidate or protect their full quantities.
    A mismatch does not establish whether a split, transfer or external trade
    caused it. Broker activity and corporate-action records require review.
    """
    expected = journal.expected_positions_decimal_from_fills()
    actual: dict[str, Decimal] = {}
    messages: list[str] = []
    issues: list[ReconciliationIssue] = []

    def issue(code: str, symbol: str, expected_qty: Decimal | None,
              actual_qty: Decimal | None, message: str, action: str) -> None:
        issues.append(ReconciliationIssue(code, symbol,
                                         str(expected_qty) if expected_qty is not None else None,
                                         str(actual_qty) if actual_qty is not None else None, action))
        messages.append(f"{message}. Action: {action}")

    for raw_symbol, position in broker.positions.items():
        symbol = str(raw_symbol).upper()
        raw_quantity = getattr(position, "broker_quantity", None)
        if raw_quantity is None:
            raw_quantity = position.quantity
        try:
            quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, ValueError):
            quantity = Decimal("NaN")
        if not quantity.is_finite():
            issue("invalid_broker_quantity", symbol, expected.get(symbol), None,
                  f"{symbol}: Alpaca reported an invalid holding quantity",
                  "Verify the broker position before restarting trading")
            continue
        if symbol in actual:
            issue("duplicate_broker_symbol", symbol, expected.get(symbol), quantity,
                  f"{symbol}: duplicate broker position identity",
                  "Verify symbol identity and the broker snapshot before restarting trading")
            continue
        if quantity:
            actual[symbol] = quantity

    for symbol, expected_qty in sorted(expected.items()):
        actual_qty = actual.get(symbol, Decimal(0))
        if actual_qty != expected_qty:
            issue("position_quantity_mismatch", symbol, expected_qty, actual_qty,
                  f"{symbol}: bot expected {expected_qty} shares, Alpaca shows {actual_qty}",
                  "Review broker fills, transfers, splits, mergers and symbol changes; "
                  "reconcile the ledger explicitly without resetting or guessing an adjustment")

    for symbol, actual_qty in sorted(actual.items()):
        if symbol not in expected:
            issue("untracked_broker_position", symbol, Decimal(0), actual_qty,
                  f"{symbol}: Alpaca shows {actual_qty} shares not recorded in bot fills",
                  "Review external trades, transfers and corporate actions; use a dedicated account "
                  "and reconcile ownership before restarting trading")
        if actual_qty < 0:
            issue("unsupported_short_position", symbol, expected.get(symbol, Decimal(0)), actual_qty,
                  f"{symbol}: short holdings are unsupported",
                  "Resolve the short position with the broker; automated long-only exits cannot protect it")
        if actual_qty != actual_qty.to_integral_value():
            issue("unsupported_fractional_position", symbol, expected.get(symbol, Decimal(0)), actual_qty,
                  f"{symbol}: fractional holdings are unsupported by whole-share exits and protective stops",
                  "Review partial fills and corporate-action records, then resolve the fractional remainder "
                  "with the broker; do not round it away or assume it has protective stop coverage")

    # A negative/fractional journal quantity remains diagnostic even if the
    # broker is flat; never erase a malformed or incomplete execution history.
    for symbol, expected_qty in sorted(expected.items()):
        if expected_qty < 0 and actual.get(symbol, Decimal(0)) >= 0:
            issue("negative_execution_balance", symbol, expected_qty, actual.get(symbol, Decimal(0)),
                  f"{symbol}: confirmed executions imply a negative holding balance",
                  "Review missing opening fills and execution corrections before restarting trading")

    return ReconciliationResult(ok=not issues, messages=messages, issues=issues)
