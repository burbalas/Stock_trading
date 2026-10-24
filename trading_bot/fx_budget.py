"""Dated EUR/USD reference conversion for budgets; never executes FX trades.

ECB daily reference rates are informational marks. They are not executable
currency quotes and do not include a broker's conversion spread or fees.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
from pathlib import Path
import tempfile
import time
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .atomic_storage import cleanup_temporary, replace_file


ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_NAMESPACE = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"
_ENVELOPE_TAG = "{http://www.gesmes.org/xml/2002-08-01}Envelope"
MAX_REFERENCE_AGE_DAYS = 4
MAX_RESPONSE_BYTES = 65_536
_CACHE_RECHECK_SECONDS = 3_600
_BUDGET_TIMEZONE = ZoneInfo("Europe/Vilnius")


def _current_date() -> date:
    return datetime.now(_BUDGET_TIMEZONE).date()


def _decimal(value, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{label} must be a finite decimal number.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal number.") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite decimal number.")
    return result


@dataclass(frozen=True)
class FxQuote:
    usd_per_eur: Decimal | str | float
    as_of: date
    source: str = ECB_DAILY_URL
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        rate = _decimal(self.usd_per_eur, "USD per EUR")
        if rate <= 0:
            raise ValueError("USD per EUR must be positive.")
        if not isinstance(self.as_of, date) or isinstance(self.as_of, datetime):
            raise ValueError("FX reference date must be a date.")
        if self.source != ECB_DAILY_URL:
            raise ValueError("FX source must be the pinned ECB daily reference feed.")
        if self.fetched_at is not None and (
            not isinstance(self.fetched_at, datetime)
            or self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None
        ):
            raise ValueError("FX fetch timestamp must have a timezone.")
        object.__setattr__(self, "usd_per_eur", rate)

    def _validated_age(self, today: date | None = None) -> int:
        today = today or _current_date()
        if not isinstance(today, date) or isinstance(today, datetime):
            raise ValueError("FX freshness requires a date.")
        age = (today - self.as_of).days
        if age < 0:
            raise ValueError("FX reference date is in the future.")
        if self.fetched_at is not None:
            fetched_day = self.fetched_at.astimezone(_BUDGET_TIMEZONE).date()
            if (fetched_day > today or fetched_day < self.as_of
                    or self.fetched_at.astimezone(timezone.utc) > datetime.now(timezone.utc)):
                raise ValueError("FX fetch timestamp is future-dated or precedes its reference date.")
        return age

    def validate(self, *, today: date | None = None, max_age_days: int = MAX_REFERENCE_AGE_DAYS) -> "FxQuote":
        if (isinstance(max_age_days, bool) or not isinstance(max_age_days, int)
                or not 0 <= max_age_days <= MAX_REFERENCE_AGE_DAYS):
            raise ValueError("FX freshness requires an age limit from zero to four days.")
        age = self._validated_age(today)
        if age > max_age_days:
            raise ValueError(f"FX reference is stale ({age} days; maximum {max_age_days}).")
        return self

    def eur_to_usd(self, amount, *, buffer_bps=0) -> Decimal:
        """Convert a nonnegative EUR allocation, rounding the USD cap down.

        A buffer reduces the deployable amount; it is an optional reserve and
        does not estimate or guarantee an executable currency conversion.
        Call validate at the operating date before applying a stored quote.
        """
        euros = _decimal(amount, "EUR amount")
        buffer = _decimal(buffer_bps, "FX buffer")
        if euros < 0 or not 0 <= buffer < 10_000:
            raise ValueError("EUR amount must be nonnegative and FX buffer must be from zero to below 10000 bps.")
        return (euros * self.usd_per_eur * (Decimal(1) - buffer / Decimal(10_000))).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN,
        )

    def usd_to_eur(self, amount) -> Decimal:
        """Mark a signed USD amount in EUR at the daily reference rate."""
        return _decimal(amount, "USD amount") / self.usd_per_eur

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "usd_per_eur": str(self.usd_per_eur),
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at is not None else None,
        }

    @classmethod
    def _decode(cls, value: dict) -> "FxQuote":
        if (not isinstance(value, dict) or type(value.get("version", 1)) is not int
                or value.get("version", 1) != 1):
            raise ValueError("FX reference must be a version one object.")
        if value.get("base_currency", "EUR") != "EUR" or value.get("quote_currency", "USD") != "USD":
            raise ValueError("FX reference must state USD per EUR, not its inverse.")
        try:
            as_of = date.fromisoformat(value["as_of"])
            fetched = value.get("fetched_at")
            fetched_at = datetime.fromisoformat(fetched) if fetched is not None else None
            quote = cls(value["usd_per_eur"], as_of, value.get("source", ECB_DAILY_URL), fetched_at)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid EUR/USD reference quote: {exc}") from exc
        return quote

    @classmethod
    def from_dict(cls, value: dict, *, today: date | None = None) -> "FxQuote":
        return cls._decode(value).validate(today=today)


def _parse_ecb_xml(raw: bytes, *, fetched_at: datetime, today: date) -> FxQuote:
    if len(raw) > MAX_RESPONSE_BYTES or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("ECB reference response is oversized or contains unsupported XML declarations.")
    try:
        document = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("ECB reference response is not valid XML.") from exc
    if document.tag != _ENVELOPE_TAG:
        raise ValueError("ECB reference response has an unexpected envelope.")
    days = [cube for cube in document.iter(f"{{{_ECB_NAMESPACE}}}Cube") if "time" in cube.attrib]
    if len(days) != 1:
        raise ValueError("ECB daily reference must have exactly one dated observation.")
    rates = [row for row in days[0] if row.tag == f"{{{_ECB_NAMESPACE}}}Cube" and row.get("currency") == "USD"]
    if len(rates) != 1:
        raise ValueError("ECB daily reference must have exactly one USD per EUR rate.")
    try:
        quote = FxQuote(rates[0].attrib["rate"], date.fromisoformat(days[0].attrib["time"]),
                        fetched_at=fetched_at)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ECB EUR/USD reference: {exc}") from exc
    return quote.validate(today=today)


def _fetch_ecb_quote(*, today: date) -> FxQuote:
    import requests

    deadline = time.monotonic() + 20
    with requests.get(ECB_DAILY_URL, timeout=(5, 15), allow_redirects=False, stream=True,
                      headers={"User-Agent": "StockTrading/1.0 reference-fx"}) as response:
        if response.status_code != 200:
            raise RuntimeError(f"ECB reference request returned HTTP {response.status_code}; redirects are refused.")
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=1_024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("ECB reference response exceeds 64 KiB.")
            if time.monotonic() > deadline:
                raise TimeoutError("ECB reference download exceeded its time limit.")
            chunks.append(chunk)
    return _parse_ecb_xml(b"".join(chunks), fetched_at=datetime.now(timezone.utc), today=today)


def _write_cache(path: Path, quote: FxQuote) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(quote.to_dict(), handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
    finally:
        if temporary is not None:
            cleanup_temporary(temporary)


def _read_cache(path: Path) -> FxQuote:
    if path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ValueError("FX cache exceeds 64 KiB.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate FX reference field: {key}")
            result[key] = value
        return result

    return FxQuote._decode(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique))


def load_cached_reference_for_risk_only(cache_path: str | Path, *, today: date | None = None) -> FxQuote:
    """Recover a valid historical mark for existing-position risk exits only.

    This deliberately permits a stale reference while an FX service is down.
    Dates, rate direction, source and finite positive rate remain mandatory;
    future references are refused. It performs no network call. The caller
    must label the mark stale and refuse new entries or first initialization
    until quote.validate() succeeds. A historical mark does not establish a
    current EUR loss bound, and must not prevent protective USD risk exits.
    """
    quote = _read_cache(Path(cache_path))
    quote._validated_age(today)
    return quote


def load_ecb_reference_quote(cache_path: str | Path | None = None, *, today: date | None = None,
                             force_refresh: bool = False) -> FxQuote:
    """Load only the pinned ECB feed, retaining a still-fresh cache on outages.

    Cache is rechecked at least hourly. Weekends and bank holidays allow up to
    four calendar days; a stale or future quote never authorizes a budget.
    No broker credentials, account values, FX orders, or executable rates are
    sent to the reference service.
    """
    today = today or _current_date()
    cache = Path(cache_path) if cache_path is not None else None
    cached = None
    if cache is not None and cache.exists():
        try:
            cached = _read_cache(cache).validate(today=today)
        except (OSError, ValueError, TypeError):
            cached = None
    if cached is not None and not force_refresh and cached.fetched_at is not None:
        elapsed = (datetime.now(timezone.utc) - cached.fetched_at.astimezone(timezone.utc)).total_seconds()
        if 0 <= elapsed <= _CACHE_RECHECK_SECONDS:
            return cached
    try:
        quote = _fetch_ecb_quote(today=today)
    except Exception as exc:
        if cached is not None:
            return cached
        raise RuntimeError(f"No fresh ECB EUR/USD reference is available: {exc}") from exc
    if cache is not None:
        _write_cache(cache, quote)
    return quote
