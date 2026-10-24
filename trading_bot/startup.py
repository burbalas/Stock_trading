"""Opt-in, per-user Windows logon startup for the local paper application."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from .atomic_storage import cleanup_temporary, replace_file


_FILENAME = "StockTradingPaper.cmd"
_OWNER = "@rem StockTradingPaper startup v1"
_UNSAFE_PATH_CHARACTERS = frozenset('"%!^<>|')


def _windows() -> bool:
    return os.name == "nt"


def _safe_path(value, label: str) -> Path:
    path = Path(value)
    rendered = str(path)
    if (not path.is_absolute() or any(ord(character) < 32 for character in rendered)
            or any(character in _UNSAFE_PATH_CHARACTERS for character in rendered)):
        raise RuntimeError(
            f"{label} must be an absolute path without control characters, quotes, %, !, ^, <, > or |."
        )
    # Do not follow a link into another startup file or executable. This also
    # rejects Windows junctions and other filesystem reparse points.
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise RuntimeError(f"{label} cannot contain symbolic links or reparse points.")
    return path


def _startup_file(*, required: bool) -> Path | None:
    if not _windows():
        if required:
            raise RuntimeError("Automatic logon startup is supported only on Windows.")
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        if required:
            raise RuntimeError("Windows APPDATA is unavailable; automatic logon startup cannot be configured.")
        return None
    base = _safe_path(appdata, "Windows APPDATA")
    return _safe_path(base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / _FILENAME,
                      "Startup file")


def _owned(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readline(200).rstrip("\r\n") == _OWNER
    except UnicodeError:
        return False


def _command_argument(path: Path) -> str:
    # Windows argument parsing treats backslashes before the closing quote as
    # escapes. Drive/share roots retain a trailing slash, so double it here.
    rendered = str(path)
    trailing = len(rendered) - len(rendered.rstrip("\\"))
    return '"' + rendered + "\\" * trailing + '"'


def startup_enabled() -> bool:
    """Whether this application's owned logon startup entry currently exists."""
    try:
        path = _startup_file(required=False)
        return bool(path and path.is_file() and _owned(path))
    except (OSError, RuntimeError):
        return False


def set_startup(enabled: bool, project_dir: Path, runtime_dir: Path) -> None:
    """Install/remove only our own startup entry, following an explicit UI choice.

    Startup calls ``paper_app resume``; it cannot enable trading when the
    application's persisted desired state is disabled or its STOP flag exists.
    No credentials or account data are placed in the startup entry.
    """
    if not isinstance(enabled, bool):
        raise ValueError("Startup enabled must be a boolean.")
    destination = _startup_file(required=enabled)
    if destination is None:
        return
    if destination.exists() and (not destination.is_file() or not _owned(destination)):
        raise RuntimeError(f"Refusing to change a startup entry not owned by StockTrading: {destination}")
    if not enabled:
        destination.unlink(missing_ok=True)
        return

    project = _safe_path(project_dir, "Project directory")
    runtime = _safe_path(runtime_dir, "Runtime directory")
    if not project.is_dir():
        raise RuntimeError("Project directory is missing; logon startup was not installed.")
    if runtime.exists() and not runtime.is_dir():
        raise RuntimeError("Runtime directory must be a directory; logon startup was not installed.")
    python = _safe_path(project / ".venv" / "Scripts" / "pythonw.exe", "Project Python")
    if not python.is_file():
        raise RuntimeError("Project .venv/Scripts/pythonw.exe is missing; logon startup was not installed.")

    content = "\r\n".join((
        _OWNER,
        "@echo off",
        "chcp 65001 >nul",
        "setlocal DisableDelayedExpansion",
        f'cd /d "{project}"',
        "if errorlevel 1 exit /b 1",
        f'start "" {_command_argument(python)} -m trading_bot.paper_app resume '
        f'--project-dir {_command_argument(project)} --runtime-dir {_command_argument(runtime)}',
        "",
    ))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".stocktrading-startup-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # A file created by another application while preparing the entry must
        # remain untouched. The final write replaces only an owned entry.
        _safe_path(destination, "Startup file")
        if destination.exists() and (not destination.is_file() or not _owned(destination)):
            raise RuntimeError(f"Refusing to replace a startup entry not owned by StockTrading: {destination}")
        replace_file(temporary, destination)
    finally:
        cleanup_temporary(temporary)
