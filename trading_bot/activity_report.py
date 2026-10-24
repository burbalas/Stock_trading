"""Offline activity summaries that keep previews and evaluation runs separate."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .atomic_storage import cleanup_temporary, replace_file
from .journal import TradeJournal
from .time_utils import LOCAL_TZ, now_local


def _text(value) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _number(value, context: str, *, positive=False, nonnegative=False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{context}: expected a finite {'positive' if positive else 'nonnegative' if nonnegative else ''} number.") from exc


def _timestamp(value, context: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: expected a timezone-aware ISO timestamp.") from exc


def _date(value, context: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: expected an ISO session date.") from exc


def _csv_rows(path: Path, required: set[str]) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle, strict=True)
            headers = reader.fieldnames
            if not headers or len(headers) != len(set(headers)) or not required.issubset(headers):
                raise ValueError(f"{path.name}: missing or duplicate required CSV headers.")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"{path.name} row {reader.line_num}: malformed CSV row.")
                row["_line"] = reader.line_num
                rows.append(row)
            return rows
    except (csv.Error, UnicodeError) as exc:
        raise ValueError(f"{path.name}: malformed or non-UTF-8 CSV data.") from exc


def _observations(directory: Path) -> list[dict]:
    required = {"timestamp", "market_date", "account_id", "mode", "phase", "strategy",
                "equity", "parameters_sha256", "parameters_json"}
    rows = _csv_rows(directory / "account_observations.csv", required)
    for row in rows:
        context = f"account_observations.csv row {row['_line']}"
        row["_timestamp"] = _timestamp(row["timestamp"], context)
        row["_market_date"] = _date(row["market_date"], context) if row["market_date"] else None
        row["_equity"] = _number(row["equity"], context, nonnegative=True)
        if row["mode"] not in {"preview", "paper-submit", "live-submit"} or row["phase"] not in {"risk", "signal"}:
            raise ValueError(f"{context}: invalid observation mode or phase.")
        if not row["strategy"].strip():
            raise ValueError(f"{context}: missing strategy name.")
        fingerprint = row["parameters_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"{context}: invalid parameters SHA-256 fingerprint.")
        try:
            parameters = json.loads(row["parameters_json"])
            if not isinstance(parameters, dict):
                raise ValueError
            json.dumps(parameters, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{context}: invalid parameter JSON.") from exc
        if hashlib.sha256(row["parameters_json"].encode("utf-8")).hexdigest() != fingerprint:
            raise ValueError(f"{context}: parameter fingerprint does not match the recorded settings.")
    return rows


def _fills(directory: Path) -> list[dict]:
    rows = _csv_rows(directory / "fills.csv", {"timestamp", "mode", "date", "symbol", "side",
                                                "quantity", "price", "value"})
    for row in rows:
        context = f"fills.csv row {row['_line']}"
        _timestamp(row["timestamp"], context)
        row["_date"] = _date(row["date"], context)
        if not row["mode"].strip() or not row["symbol"].strip() or row["side"] not in {"buy", "sell"}:
            raise ValueError(f"{context}: missing mode/symbol or invalid fill side.")
        quantity = _number(row["quantity"], context, positive=True)
        price = _number(row["price"], context, positive=True)
        row["_value"] = _number(row["value"], context, positive=True)
        expected_value = quantity * price
        if not math.isfinite(expected_value) or not math.isclose(
                expected_value, row["_value"], rel_tol=1e-8, abs_tol=.02):
            raise ValueError(f"{context}: fill value does not match quantity times price.")
    return rows


def _drawdown(rows: list[dict]) -> float | None:
    if len(rows) < 2 or not any(row["_equity"] > 0 for row in rows):
        return None
    peak, worst = 0.0, 0.0
    for row in rows:
        equity = row["_equity"]
        peak = max(peak, equity)
        if peak:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _total_fill_value(rows: list[dict]) -> float:
    try:
        return _number(math.fsum(row["_value"] for row in rows), "fills.csv total execution value", nonnegative=True)
    except OverflowError as exc:
        raise ValueError("fills.csv total execution value exceeds a finite number.") from exc


def write_activity_report(runtime_dir, broker_snapshot: dict, runtime_status: dict) -> Path:
    """Write a local Markdown report from recorded data, without broker requests.

    Equity measurements have no deposit/withdrawal ledger and are never called
    investment returns. Only submitting observations tied to the journal's
    confirmed account can appear in forward-paper cohorts. Every distinct
    strategy/parameter fingerprint stays separate.
    """
    if not isinstance(broker_snapshot, dict) or not isinstance(runtime_status, dict):
        raise ValueError("Broker snapshot and runtime status must be objects.")
    directory = Path(runtime_dir)
    journal = TradeJournal(directory)
    with journal.lock():
        state = journal._read_state()
        account_id = state.get("account_id")
        if account_id is not None and (not isinstance(account_id, str) or not account_id.strip()):
            raise ValueError("bot_state.json: current account ID is invalid.")
        observations = _observations(directory)
        fills = _fills(directory)

    now = now_local()
    current = [row for row in observations if account_id and row["account_id"] == account_id]
    forward = [row for row in current if row["mode"] == "paper-submit"]
    previews = [row for row in current if row["mode"] == "preview"]
    live = [row for row in current if row["mode"] == "live-submit"]
    excluded = len(observations) - len(current)
    cohorts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in forward:
        cohorts[(row["strategy"], row["parameters_sha256"])].append(row)
    market_dates = sorted({row["_market_date"] for row in forward if row["_market_date"]})

    operator = runtime_status.get("operator", {})
    supervisor = runtime_status.get("supervisor", {})
    if not isinstance(operator, dict) or not isinstance(supervisor, dict):
        raise ValueError("Runtime operator/supervisor status must be objects.")
    operator_state = _text(operator.get("state", "unverified"))
    lines = ["# Trading bot activity", "",
             f"Generated: {now.isoformat(timespec='seconds')} (Europe/Vilnius).",
             f"Operator activity: **{operator_state}** ({_text(operator.get('detail', 'no verified heartbeat'))}).",
             f"Supervisor: {_text(supervisor.get('status', 'unverified'))}."]
    if operator.get("updated_at"):
        stamp = _timestamp(operator["updated_at"], "Operator heartbeat")
        lines.append(f"Last operator heartbeat: {stamp.isoformat()}.")
    if runtime_status.get("paused"):
        lines.append("New buys are paused.")
    if runtime_status.get("stop_requested"):
        lines.append("A stop has been requested.")
    if supervisor.get("last_error"):
        lines.append(f"Last supervisor error: {_text(supervisor['last_error'])}.")
    lines += ["", "## Evidence summary", ""]
    if not forward:
        lines.append("**Evidence insufficient:** no forward paper observations are tied to the current journal account.")
    else:
        lines.append(f"Recorded forward paper activity: **{len(forward)} observations** across "
                     f"**{len(market_dates)} recorded market dates**, in **{len(cohorts)} separate experiments**.")
        lines.append("These observations do not establish profitable trading.")
    lines.append(f"Preview observations for the current account: {len(previews)}. Previews are excluded from forward paper evidence.")
    if live:
        lines.append(f"Live-money observations for the current account: {len(live)}. These are separate from all paper experiments and are not audited investment returns.")
    lines.append(f"Other-account or unbound observations excluded: {excluded}.")
    if not account_id:
        lines.append("The journal has no confirmed account ID; observation attribution is unverified.")
    if market_dates:
        lines.append(f"Recorded market-date range: {market_dates[0]} through {market_dates[-1]}.")
        recent_dates = market_dates[-20:]
        lines.append(("Recorded market dates: " if len(market_dates) <= 20 else "Latest 20 recorded market dates: ")
                     + ", ".join(day.isoformat() for day in recent_dates) + ".")
    undated = sum(row["_market_date"] is None for row in forward)
    if undated:
        lines.append(f"Submitting observations without a recorded market date: {undated}; they do not increase the session-date count.")

    lines += ["", "## Separate paper experiments", "",
              "First/latest equity and drawdown below are observed account measurements, **not cash-flow-adjusted returns**. "
              "Deposits and withdrawals can change them. Sampling may miss losses between observations.", ""]
    if not cohorts:
        lines.append("No attributable submitting observations are available. No profit or drawdown is inferred.")
    for index, ((strategy, fingerprint), rows) in enumerate(sorted(cohorts.items()), 1):
        rows.sort(key=lambda row: row["_timestamp"])
        sessions = sorted({row["_market_date"] for row in rows if row["_market_date"]})
        worst = _drawdown(rows)
        lines += [f"### Experiment {index}: {_text(strategy)}", "",
                  f"Parameters SHA-256: `{fingerprint}`.",
                  f"Observations: {len(rows)} ({sum(row['phase'] == 'risk' for row in rows)} risk, "
                  f"{sum(row['phase'] == 'signal' for row in rows)} signal); recorded market dates: {len(sessions)}.",
                  f"First observed equity: {_money(rows[0]['_equity'])} at {rows[0]['_timestamp'].isoformat()}.",
                  f"Latest observed equity: {_money(rows[-1]['_equity'])} at {rows[-1]['_timestamp'].isoformat()}.",
                  (f"Maximum observed drawdown: {worst:.2%}." if worst is not None else
                   "Maximum observed drawdown: unavailable; at least two observations and a positive equity peak are needed."), ""]

    lines += ["## Cached broker snapshot", "",
              "This cached brokerage check is separate from the observation ledger and does not prove the bot is running."]
    if broker_snapshot.get("updated_at"):
        stamp = _timestamp(broker_snapshot["updated_at"], "Cached broker snapshot")
        age_seconds = (now - stamp.astimezone(LOCAL_TZ)).total_seconds()
        freshness = "stale" if age_seconds > 600 else "future timestamp; unverified" if age_seconds < -60 else "recent"
        lines.append(f"Broker snapshot recorded at: {stamp.isoformat()} ({freshness}).")
    else:
        lines.append("Broker snapshot timestamp: unavailable; freshness is unverified.")
    if account_id:
        lines.append(f"Current journal account suffix: `{_text(account_id[-8:])}`.")
    if broker_snapshot.get("account_suffix"):
        suffix = str(broker_snapshot["account_suffix"])
        lines.append(f"Cached broker account suffix: `{_text(suffix)}`.")
        if account_id and not account_id.endswith(suffix):
            lines.append("**Cached broker account does not match the journal account; cached values are not attributed to this experiment.**")
    for field in ("equity", "cash"):
        if broker_snapshot.get(field) is not None:
            value = _number(broker_snapshot[field], f"Cached broker {field}", nonnegative=field == "equity")
            lines.append(f"Cached {field}: {_money(value)}.")
    for field, title in (("positions", "Positions"), ("open_orders", "Open orders")):
        if field in broker_snapshot:
            if not isinstance(broker_snapshot[field], list):
                raise ValueError(f"Cached broker {field} must be a list.")
            lines.append(f"{title} in cached snapshot: {len(broker_snapshot[field])}.")
    reconciliation = broker_snapshot.get("reconciliation_ok")
    if reconciliation is not None and not isinstance(reconciliation, bool):
        raise ValueError("Cached broker reconciliation status must be a boolean.")
    lines.append(f"Cached reconciliation: {'passed' if reconciliation is True else 'FAILED' if reconciliation is False else 'unverified'}.")
    if broker_snapshot.get("error"):
        lines.append(f"Broker snapshot error: {_text(broker_snapshot['error'])}.")

    lines += ["", "## Fill ledger totals (unattributed)", "",
              "All legacy and later fill rows in this file are included. Account and parameter attribution is unavailable; "
              "these totals are **not attributed to the current candidate**.",
              f"Recorded fills: **{len(fills)}**. Fills are execution records, not completed round trips."]
    if fills:
        buy_rows = [row for row in fills if row["side"] == "buy"]
        sell_rows = [row for row in fills if row["side"] == "sell"]
        lines += [f"Fill dates: {min(row['_date'] for row in fills)} through {max(row['_date'] for row in fills)}.",
                  f"Buy fills: {len(buy_rows)}; execution value: {_money(_total_fill_value(buy_rows))}.",
                  f"Sell fills: {len(sell_rows)}; execution value: {_money(_total_fill_value(sell_rows))}.",
                  "Execution values are turnover, not profit. No realized P/L is calculated in this activity report."]
    else:
        lines.append("No fills are recorded. No trading profit is inferred.")

    reports = directory / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    destination = reports / f"activity-{now.date().isoformat()}.md"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=reports,
                                         prefix=".activity-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write("\n".join(lines).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, destination)
    finally:
        if temporary is not None:
            cleanup_temporary(temporary)
    return destination
