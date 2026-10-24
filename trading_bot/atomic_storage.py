"""Atomic file replacement that tolerates brief Windows sharing conflicts."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time


# Ten attempts, at most 5.55 seconds of waiting. The caller retains its lock
# and the same completed temporary file throughout; no business action repeats.
_RETRY_DELAYS = (.05, .10, .20, .40, .80, 1.0, 1.0, 1.0, 1.0)
_WINDOWS_SHARING_ERRORS = {5, 32, 33}


def replace_file(source: str | Path, destination: str | Path) -> None:
    """Replace without truncating/unlinking the previous destination on failure.

    Sync clients, scanners and other readers can briefly prevent replacement
    on Windows. Persistent access denial and all other errors still propagate.
    Callers must close and flush the source before entering this function.
    """
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in _WINDOWS_SHARING_ERRORS or attempt == len(_RETRY_DELAYS):
                raise
            time.sleep(_RETRY_DELAYS[attempt])


def cleanup_temporary(path: str | Path) -> None:
    """Remove our temporary file without hiding a pending write exception."""
    primary_error = sys.exc_info()[1]
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        if primary_error is None:
            raise
        primary_error.add_note(f'Temporary file cleanup also failed for {path}: {exc}')
