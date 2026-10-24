"""Canonical local paper storage and conservative migration from project state.

The caller must hold the user-wide paper execution lock during migration.
Journal locks additionally prevent an already-running legacy worker from being
copied. The source is retained as an archive; a redirect is the final commit.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .atomic_storage import cleanup_temporary, replace_file


_REDIRECT = ".runtime_redirect.json"
_MIGRATION = ".runtime_migration.json"
_MAX_REDIRECTS = 4
_STATE_FILES = frozenset({
    "bot_state.json", "fills.csv", "decisions.csv", "account_observations.csv",
    "heartbeat.json", "STOP", "STOP_BUYS",
})


def runtime_home() -> Path:
    """Return user-local storage, independently of the project checkout."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    return (_absolute(base) if base else Path.home() / ".cache") / "StockTrading"


def default_paper_directory() -> Path:
    return runtime_home() / "paper"


def _absolute(path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _check_path(path: Path) -> None:
    """Refuse links/reparse points rather than following them while copying."""
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise RuntimeError(f"Runtime paths must not contain symbolic links or reparse points: {component}")


def canonical_runtime_directory(path) -> Path:
    """Resolve an existing directory only after rejecting links in its input.

    Windows Store Python can expose a user-local directory through both its
    logical AppData path and a physical package-cache path, without a symbolic
    link. Resolve again after creation to keep those aliases on one journal.
    """
    original = _absolute(path)
    _check_path(original)
    if not original.exists():
        return original
    if not original.is_dir():
        raise RuntimeError(f"Runtime path is not a directory: {original}")
    resolved = original.resolve(strict=True)
    _check_path(resolved)
    return resolved


def _same_directory(first, second) -> bool:
    first = canonical_runtime_directory(first)
    second = canonical_runtime_directory(second)
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return False


def _read_json(path: Path) -> dict:
    _check_path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Runtime metadata is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Runtime metadata must be a JSON object: {path}")
    return payload


def resolve_journal_directory(path) -> Path:
    """Follow a validated migration redirect, refusing cycles or missing targets."""
    current = canonical_runtime_directory(path)
    visited = set()
    for depth in range(_MAX_REDIRECTS + 1):
        _check_path(current)
        identity = os.path.normcase(str(current))
        if identity in visited:
            raise RuntimeError("Runtime redirect cycle detected; journal access refused.")
        visited.add(identity)
        marker = current / _REDIRECT
        _check_path(marker)
        if not marker.exists():
            return current
        if depth == _MAX_REDIRECTS:
            raise RuntimeError("Runtime redirect chain exceeds four redirects; journal access refused.")
        payload = _read_json(marker)
        target = payload.get("target")
        if (type(payload.get("version")) is not int or payload["version"] != 1
                or not isinstance(target, str) or not target.strip()
                or not Path(target).is_absolute()):
            raise RuntimeError(f"Invalid runtime redirect schema: {marker}")
        current = canonical_runtime_directory(target)
        if not current.is_dir():
            raise RuntimeError(f"Runtime redirect target is missing or not a directory: {current}")
    raise AssertionError("Unreachable redirect traversal")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(directory: Path) -> dict[str, str]:
    selected = {}
    for name in sorted(_STATE_FILES):
        path = directory / name
        _check_path(path)
        if path.exists():
            if not path.is_file():
                raise RuntimeError(f"Expected a runtime file: {path}")
            selected[name] = _digest(path)
    for name in ("archive", "logs"):
        root = directory / name
        _check_path(root)
        if not root.exists():
            continue
        if not root.is_dir():
            raise RuntimeError(f"Expected a runtime directory: {root}")
        for parent, directories, files in os.walk(root, followlinks=False):
            for child in (*directories, *files):
                _check_path(Path(parent) / child)
            for child in sorted(files):
                # Reset archives contain authoritative filenames. Console logs
                # have .log suffixes. Never copy arbitrary JSON/CSV or secrets.
                allowed = child.endswith(".log") if name == "logs" else child in _STATE_FILES
                if allowed:
                    path = Path(parent) / child
                    if not path.is_file():
                        raise RuntimeError(f"Expected a regular runtime file: {path}")
                    selected[path.relative_to(directory).as_posix()] = _digest(path)
    return dict(sorted(selected.items()))


def _write_json(path: Path, payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".migration-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
    finally:
        cleanup_temporary(temporary)


def _copy_verified(source: Path, destination: Path, expected: str) -> None:
    _check_path(destination)
    if destination.exists():
        if not destination.is_file() or _digest(destination) != expected:
            raise RuntimeError(f"Runtime migration conflict; existing data was preserved: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".migration-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        if _digest(Path(temporary)) != expected:
            raise RuntimeError(f"Source changed while copying; migration was not committed: {source}")
        if destination.exists():
            if not destination.is_file() or _digest(destination) != expected:
                raise RuntimeError(f"Runtime migration conflict; existing data was preserved: {destination}")
            return
        replace_file(temporary, destination)
    finally:
        cleanup_temporary(temporary)


def prepare_paper_runtime(legacy_dir, target_dir) -> Path:
    """Copy an unchanged legacy journal and commit its canonical redirect last.

    Differing destination ledgers are never merged or reset. An interrupted
    migration can resume only when its source inventory and completed copies
    still match. The caller owns the broader user-wide paper execution lock.
    """
    source = canonical_runtime_directory(legacy_dir)
    target = canonical_runtime_directory(target_dir)
    if _same_directory(source, target):
        return resolve_journal_directory(source)
    if source in target.parents or target in source.parents:
        raise RuntimeError("Legacy and local runtime directories must not contain one another.")
    if (source / _REDIRECT).exists():
        resolved = resolve_journal_directory(source)
        if not _same_directory(resolved, target):
            raise RuntimeError("Legacy journal already redirects to a different runtime directory.")
        return resolved
    if resolve_journal_directory(target) != target:
        raise RuntimeError("Migration target must be a canonical runtime directory.")
    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    # Store-Python filesystem virtualization may become visible to resolve()
    # only once the directory exists. Commit the physical path from now on.
    source = canonical_runtime_directory(source)
    target = canonical_runtime_directory(target)
    if _same_directory(source, target):
        return resolve_journal_directory(source)
    if source in target.parents or target in source.parents:
        raise RuntimeError("Legacy and local runtime directories must not contain one another.")
    # Lazy import permits TradeJournal to resolve migrated paths in __init__.
    from .journal import TradeJournal

    with ExitStack() as stack:
        for directory in sorted((source, target), key=lambda item: os.path.normcase(str(item))):
            journal = TradeJournal(directory)
            stack.enter_context(journal.lock("execution", timeout=0))
            stack.enter_context(journal.lock("state", timeout=0))
        inventory = _inventory(source)
        target_inventory = _inventory(target)
        for name, digest in target_inventory.items():
            if name not in inventory or inventory[name] != digest:
                raise RuntimeError(f"Runtime migration conflict; existing data was preserved: {target / name}")
        checkpoint = {
            "version": 1, "source": str(source), "target": str(target),
            "source_preserved": True, "files_sha256": inventory,
        }
        metadata = target / _MIGRATION
        if metadata.exists():
            previous = _read_json(metadata)
            same_paths = all(
                isinstance(previous.get(key), str) and Path(previous[key]).is_absolute()
                and _same_directory(previous[key], checkpoint[key])
                for key in ("source", "target")
            )
            comparable = dict(previous, source=str(source), target=str(target))
            if (not same_paths or type(previous.get("version")) is not int
                    or comparable != checkpoint):
                raise RuntimeError("Interrupted runtime migration no longer matches the source; inspect both directories.")
        else:
            _write_json(metadata, checkpoint)
        for name, digest in inventory.items():
            _copy_verified(source / name, target / name, digest)
        if _inventory(source) != inventory or _inventory(target) != inventory:
            raise RuntimeError("Runtime changed during migration; redirect was not committed.")
        _write_json(source / _REDIRECT, {"version": 1, "target": str(target), "source_preserved": True})
    return target
