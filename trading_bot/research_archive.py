"""Immutable, read-only market-data archive; no trading client or order API."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
from xml.etree import ElementTree

from .alpaca_integration import _alpaca_keys, _configure_request_timeouts
from .data import validate_market_data
from .models import Candle
from .profitability_study import STOCKS, ETFS
from .time_utils import MARKET_TZ


ECB_HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
_ECB_NAMESPACE = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"
_MAX_XML_BYTES = 16 * 1024 * 1024


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def immutable_write(path: Path, contents: bytes) -> str:
    """Create a file once, or verify that an existing file is exactly equal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(contents)
    except FileExistsError:
        if path.read_bytes() != contents:
            raise RuntimeError(f"Refusing to overwrite a different research artifact: {path.name}")
    return hashlib.sha256(contents).hexdigest()


def _error_metadata(exc: Exception) -> dict:
    # Exception messages can embed request headers or identifiers. Never persist
    # or print them; only classification and HTTP status are needed for the audit.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return {"error_type": type(exc).__name__, "http_status": status if isinstance(status, int) else None,
            "detail": "Collection failed; no completeness claim is made."}


def _tracked_fetch(client, method_name: str, request) -> tuple[dict, dict]:
    """Use the official SDK while recording that pagination actually ended."""
    original_get = client.get
    page_count = 0
    terminal_page = False
    seen_tokens = set()

    def tracked_get(*args, **kwargs):
        nonlocal page_count, terminal_page
        if page_count >= 10000:
            raise RuntimeError("Market-data pagination exceeded its bound.")
        response = original_get(*args, **kwargs)
        if not isinstance(response, dict):
            raise ValueError("Expected a dictionary market-data response.")
        page_count += 1
        token = response.get("next_page_token")
        terminal_page = token is None
        if token is not None:
            if not isinstance(token, str) or token in seen_tokens:
                raise ValueError("Invalid or repeated market-data page token.")
            seen_tokens.add(token)
        return response

    client.get = tracked_get
    try:
        result = getattr(client, method_name)(request)
    finally:
        client.get = original_get
    if not page_count or not terminal_page:
        raise RuntimeError("Market-data SDK did not demonstrate complete pagination.")
    if not isinstance(result, dict):
        raise ValueError("Expected raw market-data dictionaries.")
    return result, {"pages": page_count, "terminal_page_without_next_token": terminal_page,
                    "sdk_limit": None}


def bars_csv(raw: dict, symbols: list[str], start: date, end: date) -> tuple[bytes, dict]:
    data = {}
    if set(raw) - set(symbols):
        raise ValueError("Unexpected bar response symbols.")
    for symbol in symbols:
        rows = raw.get(symbol, [])
        if not isinstance(rows, list):
            raise ValueError("Expected a list of raw bars for each symbol.")
        candles = []
        for row in rows:
            stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Bar timestamps must include an explicit timezone.")
            day = stamp.astimezone(MARKET_TZ).date()
            if not start <= day <= end:
                raise ValueError("A bar lies outside the requested market-date range.")
            candles.append(Candle(date=day, symbol=symbol, open=float(row["o"]), high=float(row["h"]),
                                  low=float(row["l"]), close=float(row["c"]), volume=float(row["v"])))
        data[symbol] = candles
    data = validate_market_data(data, symbols)
    all_dates = sorted({bar.date for rows in data.values() for bar in rows})
    # Union coverage detects symbol-specific omissions. This is not a verified
    # exchange calendar: a session missing from every symbol remains unknown.
    date_set = set(all_dates)
    per_symbol = {}
    for symbol, rows in data.items():
        missing = sorted(date_set - {row.date for row in rows})
        per_symbol[symbol] = {"bars": len(rows), "first_date": rows[0].date.isoformat(),
                              "last_date": rows[-1].date.isoformat(),
                              "missing_union_dates": [day.isoformat() for day in missing]}
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["symbol", "date", "open", "high", "low", "close", "volume"])
    for symbol in sorted(data):
        for row in data[symbol]:
            writer.writerow([symbol, row.date.isoformat(), row.open, row.high, row.low, row.close, int(row.volume)])
    return output.getvalue().encode("utf-8"), {
        "row_count": sum(len(rows) for rows in data.values()), "market_dates": len(all_dates),
        "first_date": all_dates[0].isoformat(), "last_date": all_dates[-1].isoformat(),
        "per_symbol": per_symbol,
        "union_coverage_complete": all(not row["missing_union_dates"] for row in per_symbol.values()),
        "exchange_calendar_verified": False,
        "coverage_caveat": "Common missing sessions remain unknown without an independent exchange calendar.",
    }


def fx_csv(xml_bytes: bytes, start: date, end: date) -> tuple[bytes, dict]:
    if (len(xml_bytes) > _MAX_XML_BYTES or b"<!DOCTYPE" in xml_bytes.upper()
            or b"<!ENTITY" in xml_bytes.upper()):
        raise ValueError("Unsupported or oversized ECB XML.")
    root = ElementTree.fromstring(xml_bytes)
    values = {}
    for cube in root.iter(f"{{{_ECB_NAMESPACE}}}Cube"):
        if "time" not in cube.attrib:
            continue
        day = date.fromisoformat(cube.attrib["time"])
        rates = [row.get("rate") for row in cube if row.get("currency") == "USD"]
        if len(rates) != 1 or day in values:
            raise ValueError("Duplicate or missing dated ECB USD rate.")
        try:
            value = Decimal(rates[0])
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Invalid ECB USD rate.") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("Invalid ECB USD rate.")
        values[day] = value
    retained = sorted((day, value) for day, value in values.items() if start <= day <= end)
    if not retained:
        raise ValueError("ECB response does not cover the requested dated range.")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "usd_per_eur"])
    writer.writerows((day.isoformat(), str(value)) for day, value in retained)
    return output.getvalue().encode("utf-8"), {
        "source": ECB_HISTORY_URL, "rows": len(retained),
        "requested_start": start.isoformat(), "requested_end_inclusive": end.isoformat(),
        "first_date": retained[0][0].isoformat(), "last_date": retained[-1][0].isoformat(),
        "not_executable_conversion_quote": True,
        "calendar_note": "Reference rates are not published on every calendar day; use only an earlier dated mark.",
    }


def _fetch_ecb(session) -> bytes:
    with session.get(ECB_HISTORY_URL, timeout=(5, 30), allow_redirects=False,
                     verify=True, stream=True) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError("ECB redirect or unexpected HTTP response refused.")
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > _MAX_XML_BYTES:
                raise ValueError("ECB archive response is oversized.")
            chunks.append(chunk)
        return b"".join(chunks)


def _reuse_manifest(output: Path, request: dict) -> dict | None:
    path = output / "manifest.json"
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("request") != request:
        raise RuntimeError("Existing research archive has different request parameters.")
    for name, entry in result.get("artifacts", {}).items():
        artifact = output / name
        if Path(name).name != name or not artifact.is_file():
            raise RuntimeError("Research archive artifact is missing or has an unsafe path.")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != entry.get("sha256"):
            raise RuntimeError("Research archive artifact hash differs from its manifest.")
    return result


def archive_research(output: Path, *, start: date = date(2018, 1, 1),
                     end: date = date(2026, 10, 21), symbols: list[str] | None = None,
                     stock_client=None, actions_client=None, fx_session=None) -> dict:
    """Archive completed daily history and actions without importing a broker."""
    output = Path(output)
    symbols = sorted(set(symbols if symbols is not None else STOCKS + ETFS))
    if not symbols or start > end or end >= datetime.now(MARKET_TZ).date():
        raise ValueError("Research archive requires symbols and a completed historical date range.")
    request = {"symbols": symbols, "start": start.isoformat(), "end_inclusive": end.isoformat(),
               "timeframe": "1Day", "feed": "sip", "adjustments": ["raw", "split", "all"],
               "asof": end.isoformat(), "corporate_actions_limit": None,
               "fx_start": "2026-09-08", "fx_end_inclusive": end.isoformat()}
    # The API filters on process_date, which can be the future payment date.
    # Ex-date entitlement before `end` can therefore require a later process date.
    # Consumers must still filter economic effective/ex-dates through `end`.
    actions_end = end + timedelta(days=366)
    request["corporate_actions_process_end"] = actions_end.isoformat()
    request["fx_source"] = ECB_HISTORY_URL
    cached = _reuse_manifest(output, request)
    if cached is not None:
        return cached
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.corporate_actions import CorporateActionsClient
    from alpaca.data.requests import StockBarsRequest, CorporateActionsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    if stock_client is None or actions_client is None:
        key, secret = _alpaca_keys()
        if stock_client is None:
            stock_client = _configure_request_timeouts(StockHistoricalDataClient(key, secret, raw_data=True))
        if actions_client is None:
            actions_client = _configure_request_timeouts(CorporateActionsClient(key, secret, raw_data=True))
    if fx_session is None:
        import requests
        fx_session = requests.Session()
    result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "request": request, "artifacts": {}, "bars": {},
              "source": "Alpaca official market-data SDK and ECB dated reference XML",
              "read_only_market_data": True, "trading_api_used": False,
              "adjusted_prices_are_literal_execution_prices": False}
    for plan in ["PROFITABILITY_EVIDENCE_PLAN_2026-10-22.md", "PROFITABILITY_EVIDENCE_PLAN_ADDENDUM_2026-10-22.md"]:
        path = Path(plan)
        if path.exists():
            result.setdefault("plan_sha256", {})[plan] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["collector_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def save(name, content):
        digest = immutable_write(output / name, content)
        result["artifacts"][name] = {"sha256": digest, "bytes": len(content)}
        return name

    for adjustment in request["adjustments"]:
        try:
            params = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                start=datetime.combine(start, time.min, MARKET_TZ),
                end=datetime.combine(end + timedelta(days=1), time.min, MARKET_TZ),
                adjustment=Adjustment(adjustment), feed=DataFeed.SIP, asof=end.isoformat(), limit=None)
            raw, pages = _tracked_fetch(stock_client, "get_stock_bars", params)
            contents, coverage = bars_csv(raw, symbols, start, end)
            name = save(f"bars_{adjustment}.csv", contents)
            result["bars"][adjustment] = {"status": "ok", "file": name, "pagination": pages, **coverage}
        except Exception as exc:
            result["bars"][adjustment] = {"status": "error", **_error_metadata(exc)}
            save(f"bars_{adjustment}_error.json", _json_bytes(result["bars"][adjustment]))
    try:
        params = CorporateActionsRequest(symbols=symbols, start=start, end=actions_end, limit=None)
        raw, pages = _tracked_fetch(actions_client, "get_corporate_actions", params)
        if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
               for rows in raw.values()):
            raise ValueError("Unexpected corporate-action response shape.")
        name = save("corporate_actions.json", _json_bytes(raw))
        result["corporate_actions"] = {"status": "ok", "file": name, "pagination": pages,
            "action_counts": {key: len(rows) for key, rows in sorted(raw.items())},
            "scope": "All action types returned by the provider for the requested symbols and date filter.",
            "date_filter": "process_date; future-payable declared actions included, not future effective trading events",
            "completeness_caveat": "Pagination verified; provider omissions and the semantics of its date filter are not independently verified."}
    except Exception as exc:
        result["corporate_actions"] = {"status": "error", "complete": False, **_error_metadata(exc)}
        save("corporate_actions_error.json", _json_bytes(result["corporate_actions"]))
    try:
        xml = _fetch_ecb(fx_session)
        save("ecb_source.xml", xml)
        contents, details = fx_csv(xml, date(2026, 9, 8), end)
        name = save("fx_usd_per_eur.csv", contents)
        result["fx"] = {"status": "ok", "file": name, **details}
    except Exception as exc:
        result["fx"] = {"status": "error", **_error_metadata(exc)}
        save("fx_error.json", _json_bytes(result["fx"]))
    result["collection_succeeded"] = (all(row["status"] == "ok" for row in result["bars"].values())
        and result["corporate_actions"]["status"] == "ok" and result["fx"]["status"] == "ok")
    immutable_write(output / "manifest.json", _json_bytes(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("var/evidence_2026-10-22/data"))
    args = parser.parse_args(argv)
    result = archive_research(args.output)
    print(json.dumps({"output": str(args.output), "collection_succeeded": result["collection_succeeded"],
        "bars": {key: {"status": row["status"], "rows": row.get("row_count"), "http_status": row.get("http_status")}
                 for key, row in result["bars"].items()},
        "actions_status": result["corporate_actions"]["status"], "fx_status": result["fx"]["status"]}))
    return 0 if result["collection_succeeded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
