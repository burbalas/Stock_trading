"""Migration keeps fill evidence and never starts a worker or contacts a broker."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_bot.journal import TradeJournal
from trading_bot.runtime_paths import (
    canonical_runtime_directory, default_paper_directory, prepare_paper_runtime, resolve_journal_directory,
    runtime_home,
)
import trading_bot.runtime_paths as paths


class RuntimePathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy = self.root / "project" / "var"
        self.target = self.root / "local" / "paper"
        self.legacy.mkdir(parents=True)

    def write(self, relative, content):
        path = self.legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def redirect(self, source, target, **extra):
        source.mkdir(parents=True, exist_ok=True)
        (source / ".runtime_redirect.json").write_text(
            json.dumps({"version": 1, "target": str(target), **extra}), encoding="utf-8")

    def test_home_uses_local_app_data_and_fallback_without_project_dependency(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")}, clear=True):
            self.assertEqual(runtime_home(), self.root / "appdata" / "StockTrading")
            self.assertEqual(default_paper_directory(), runtime_home() / "paper")
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}, clear=True):
            self.assertEqual(runtime_home(), self.root / "cache" / "StockTrading")
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(runtime_home(), self.root / ".cache" / "StockTrading")

    def test_preserves_authoritative_journal_controls_archive_and_logs(self):
        evidence = {
            "bot_state.json": b'{"account_id":"paper-a","peak_equity":1001,"drawdown_halted":true}',
            "fills.csv": b"timestamp,quantity\nold,0.125\n",
            "decisions.csv": b"timestamp,reason\nold,hold\n",
            "account_observations.csv": b"equity,parameters_sha256\n1001,abc\n",
            "heartbeat.json": b'{"status":"stopped"}',
            "STOP_BUYS": b"manual pause\n", "STOP": b"manual stop\n",
            "archive/earlier/bot_state.json": b'{"account_id":"paper-old"}',
            "archive/earlier/fills.csv": b"old evidence\n",
            "logs/candidate-old.log": b"existing traceback\n",
        }
        for name, content in evidence.items():
            self.write(name, content)
        self.assertEqual(prepare_paper_runtime(self.legacy, self.target), self.target)
        for name, content in evidence.items():
            with self.subTest(name=name):
                self.assertEqual((self.legacy / name).read_bytes(), content)
                self.assertEqual((self.target / name).read_bytes(), content)
        self.assertEqual(resolve_journal_directory(self.legacy), self.target)
        self.assertEqual(prepare_paper_runtime(self.legacy, self.target), self.target)

    def test_does_not_copy_credentials_research_locks_or_temporary_files(self):
        self.write("bot_state.json", b'{"account_id":"paper-a"}')
        excluded = [".env", ".env.local", "credentials.json", "research.csv", ".execution.lock",
                    "bot_state.123.tmp", "archive/earlier/.env", "archive/earlier/credentials.json",
                    "logs/.env", "logs/incomplete.tmp", "logs/credentials.json"]
        for name in excluded:
            self.write(name, b"must remain only in source")
        prepare_paper_runtime(self.legacy, self.target)
        for name in excluded:
            if name != ".execution.lock":
                self.assertFalse((self.target / name).exists(), name)
        # Destination locks are newly created; their contents are not copied.
        self.assertEqual((self.target / ".execution.lock").read_bytes(), b"0")

    def test_differing_destination_ledger_refuses_without_reset_or_redirect(self):
        self.write("bot_state.json", b'{"account_id":"paper-a"}')
        self.write("fills.csv", b"original executions\n")
        self.target.mkdir(parents=True)
        (self.target / "fills.csv").write_bytes(b"different executions\n")
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            prepare_paper_runtime(self.legacy, self.target)
        self.assertEqual((self.target / "fills.csv").read_bytes(), b"different executions\n")
        self.assertFalse((self.legacy / ".runtime_redirect.json").exists())
        self.assertTrue((self.legacy / "bot_state.json").exists())

    def test_destination_only_ledger_is_a_conflict(self):
        self.target.mkdir(parents=True)
        (self.target / "fills.csv").write_bytes(b"another journal\n")
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            prepare_paper_runtime(self.legacy, self.target)

    def test_interrupted_copy_resumes_only_matching_completed_files(self):
        self.write("bot_state.json", b'{"account_id":"paper-a"}')
        self.write("fills.csv", b"executions\n")
        real_copy = paths._copy_verified
        calls = []

        def interrupted(source, target, digest):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("simulated disk interruption")
            return real_copy(source, target, digest)

        with patch.object(paths, "_copy_verified", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "disk interruption"):
                prepare_paper_runtime(self.legacy, self.target)
        self.assertFalse((self.legacy / ".runtime_redirect.json").exists())
        self.assertEqual((self.target / "bot_state.json").read_bytes(), (self.legacy / "bot_state.json").read_bytes())
        self.assertEqual(prepare_paper_runtime(self.legacy, self.target), self.target)
        self.assertEqual((self.target / "fills.csv").read_bytes(), b"executions\n")

    def test_changed_source_after_interruption_requires_inspection(self):
        self.write("bot_state.json", b'{"account_id":"paper-a"}')
        with patch.object(paths, "_copy_verified", side_effect=OSError("disk interruption")):
            with self.assertRaises(OSError):
                prepare_paper_runtime(self.legacy, self.target)
        self.write("STOP_BUYS", b"new control after interruption")
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            prepare_paper_runtime(self.legacy, self.target)
        self.assertFalse((self.legacy / ".runtime_redirect.json").exists())

    def test_copy_failure_never_publishes_a_partial_ledger(self):
        self.write("fills.csv", b"complete execution evidence\n")

        def interrupted_copy(input_file, output):
            output.write(input_file.read(4))
            raise OSError("simulated write failure")

        with patch.object(paths.shutil, "copyfileobj", side_effect=interrupted_copy):
            with self.assertRaisesRegex(OSError, "write failure"):
                prepare_paper_runtime(self.legacy, self.target)
        self.assertFalse((self.target / "fills.csv").exists())
        self.assertFalse((self.legacy / ".runtime_redirect.json").exists())
        self.assertEqual(list(self.target.glob(".migration-*.tmp")), [])
        # Orphan temporary files from a hard process termination are not ledgers.
        (self.target / ".migration-old.tmp").write_bytes(b"incomplete")
        prepare_paper_runtime(self.legacy, self.target)
        self.assertEqual((self.target / "fills.csv").read_bytes(), b"complete execution evidence\n")

    def test_active_legacy_execution_lock_prevents_migration(self):
        self.write("bot_state.json", b'{"account_id":"paper-a"}')
        command = [sys.executable, "-c",
                   "import sys; from trading_bot.runtime_paths import prepare_paper_runtime; "
                   "prepare_paper_runtime(sys.argv[1], sys.argv[2])", str(self.legacy), str(self.target)]
        with TradeJournal(self.legacy).lock("execution", timeout=0):
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another process", result.stderr)
        self.assertFalse((self.legacy / ".runtime_redirect.json").exists())
        self.assertFalse((self.target / "bot_state.json").exists())

    def test_missing_marker_returns_original_absolute_directory(self):
        self.assertEqual(resolve_journal_directory(self.legacy), self.legacy)
        missing = self.root / "not-created"
        self.assertEqual(resolve_journal_directory(missing), missing)
        self.assertFalse(missing.exists())

    def test_malformed_redirects_cannot_silently_open_a_new_journal(self):
        marker = self.legacy / ".runtime_redirect.json"
        invalid = ["{", "[]", "{}", json.dumps({"version": True, "target": str(self.target)}),
                   json.dumps({"version": 2, "target": str(self.target)}),
                   json.dumps({"version": 1, "target": "relative/path"}),
                   json.dumps({"version": 1, "target": str(self.target)})]
        for content in invalid:
            with self.subTest(content=content):
                marker.write_text(content, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    resolve_journal_directory(self.legacy)

    def test_redirect_cycle_and_excessive_depth_are_rejected(self):
        self.target.mkdir(parents=True)
        self.redirect(self.legacy, self.target)
        self.redirect(self.target, self.legacy)
        with self.assertRaisesRegex(RuntimeError, "cycle"):
            resolve_journal_directory(self.legacy)
        chain = [self.root / f"chain-{number}" for number in range(6)]
        for directory in chain:
            directory.mkdir()
        for source, target in zip(chain, chain[1:]):
            self.redirect(source, target)
        with self.assertRaisesRegex(RuntimeError, "four"):
            resolve_journal_directory(chain[0])
        self.assertEqual(resolve_journal_directory(chain[1]), chain[-1])

    def test_rejects_nested_directories_and_unexpected_existing_redirect(self):
        with self.assertRaisesRegex(RuntimeError, "contain one another"):
            prepare_paper_runtime(self.legacy, self.legacy / "paper")
        other = self.root / "another-runtime"
        other.mkdir()
        self.redirect(self.legacy, other)
        with self.assertRaisesRegex(RuntimeError, "different runtime"):
            prepare_paper_runtime(self.legacy, self.target)

    def test_symlinked_source_file_is_rejected_when_supported(self):
        outside = self.root / "elsewhere"
        outside.write_bytes(b"do not copy")
        link = self.legacy / "fills.csv"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links unavailable for this Windows account")
        with self.assertRaisesRegex(RuntimeError, "symbolic links"):
            prepare_paper_runtime(self.legacy, self.target)
        self.assertFalse((self.legacy / ".runtime_redirect.json").exists())

    def test_existing_redirect_accepts_verified_windows_virtualization_alias(self):
        # Store Python resolves a logical directory to its package-cache path
        # without an ordinary symlink. Simulate that behavior, not a symlink.
        logical = self.root / "logical-appdata" / "paper"
        logical.mkdir(parents=True)
        self.target.mkdir(parents=True)
        (self.target / "fills.csv").write_bytes(b"newer canonical executions\n")
        self.redirect(self.legacy, logical)
        original_resolve = Path.resolve

        def virtualized(path, *args, **kwargs):
            return self.target if path == logical else original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=virtualized):
            self.assertEqual(resolve_journal_directory(self.legacy), self.target)
            self.assertEqual(prepare_paper_runtime(self.legacy, self.target), self.target)
        self.assertEqual((self.target / "fills.csv").read_bytes(), b"newer canonical executions\n")

    def test_initial_creation_resolves_alias_before_committing_redirect_and_checkpoint(self):
        logical = self.root / "logical-paper"
        self.target.mkdir(parents=True)
        self.write("fills.csv", b"execution evidence\n")
        original_resolve = Path.resolve

        def virtualized(path, *args, **kwargs):
            return self.target if path == logical else original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=virtualized):
            self.assertFalse(logical.exists())
            self.assertEqual(prepare_paper_runtime(self.legacy, logical), self.target)
        redirect = json.loads((self.legacy / ".runtime_redirect.json").read_text())
        checkpoint = json.loads((self.target / ".runtime_migration.json").read_text())
        self.assertEqual(redirect["target"], str(self.target))
        self.assertEqual(checkpoint["target"], str(self.target))
        self.assertEqual((self.target / "fills.csv").read_bytes(), b"execution evidence\n")

    def test_interrupted_checkpoint_can_retain_identical_logical_alias_reference(self):
        logical = self.root / "logical-paper"
        logical.mkdir()
        self.target.mkdir(parents=True)
        self.write("fills.csv", b"execution evidence\n")
        checkpoint = {
            "version": 1, "source": str(self.legacy), "target": str(logical),
            "source_preserved": True, "files_sha256": paths._inventory(self.legacy),
        }
        (self.target / ".runtime_migration.json").write_text(json.dumps(checkpoint))
        original_resolve = Path.resolve

        def virtualized(path, *args, **kwargs):
            return self.target if path == logical else original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=virtualized):
            self.assertEqual(prepare_paper_runtime(self.legacy, self.target), self.target)
        self.assertEqual((self.target / "fills.csv").read_bytes(), b"execution evidence\n")
        self.assertEqual(json.loads((self.target / ".runtime_migration.json").read_text()), checkpoint)

    def test_reparse_input_is_rejected_before_any_canonicalization(self):
        original_lstat = Path.lstat

        def reparse(path, *args, **kwargs):
            if path == self.legacy:
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", autospec=True, side_effect=reparse), \
                patch.object(Path, "resolve", side_effect=AssertionError("unsafe path was resolved")):
            with self.assertRaisesRegex(RuntimeError, "reparse points"):
                canonical_runtime_directory(self.legacy)

    def test_resolved_directory_is_checked_for_reparse_points_too(self):
        unsafe = self.root / "physical-but-linked"
        original_lstat = Path.lstat

        def reparse(path, *args, **kwargs):
            if path == unsafe:
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "resolve", return_value=unsafe), \
                patch.object(Path, "lstat", autospec=True, side_effect=reparse):
            with self.assertRaisesRegex(RuntimeError, "reparse points"):
                canonical_runtime_directory(self.legacy)


if __name__ == "__main__":
    unittest.main()
