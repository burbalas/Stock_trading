from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Europe/Vilnius")
MARKET_TZ = ZoneInfo("America/New_York")


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def format_local(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_market(value: datetime) -> str:
    return value.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def utc_to_local_iso() -> str:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ).isoformat(timespec="seconds")
