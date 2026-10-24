"""Local controls and observable status for the unattended paper operator."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .atomic_storage import cleanup_temporary, replace_file


def stop_requested(journal_dir: str) -> bool:
    return (Path(journal_dir) / "STOP").exists()


def wait_for_next_check(seconds: float, journal_dir: str) -> bool:
    """Wait in short chunks so the STOP file remains effective while closed."""
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop_requested(journal_dir):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(5.0, remaining))
    return False


def write_heartbeat(journal_dir: str, status: str, **details) -> None:
    directory = Path(journal_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "status": status, **details}
    fd, temporary = tempfile.mkstemp(prefix="heartbeat-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, directory / "heartbeat.json")
    finally:
        cleanup_temporary(temporary)
