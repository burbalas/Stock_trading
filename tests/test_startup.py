"""Logon configuration tests use disposable folders, never the real Startup."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_bot.startup import set_startup, startup_enabled
import trading_bot.startup as startup


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "Stock Trading & Research (paper)"
        self.runtime = self.root / "Local App Data" / "StockTrading" / "paper"
        self.python = self.project / ".venv" / "Scripts" / "pythonw.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"offline executable placeholder")
        self.appdata = self.root / "Roaming App Data"
        self.entry = (self.appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                      / "Startup" / "StockTradingPaper.cmd")
        windows = patch.object(startup, "_windows", return_value=True)
        windows.start()
        self.addCleanup(windows.stop)
        environment = patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_read_only_status_and_disabled_missing_entry_create_nothing(self):
        self.assertFalse(startup_enabled())
        set_startup(False, self.project, self.runtime)
        self.assertFalse(self.appdata.exists())

    def test_enable_uses_fixed_executable_quoted_paths_and_resume_only(self):
        set_startup(True, self.project, self.runtime)
        self.assertTrue(startup_enabled())
        content = self.entry.read_text(encoding="utf-8")
        self.assertEqual(content.splitlines()[0], startup._OWNER)
        self.assertIn("setlocal DisableDelayedExpansion", content)
        self.assertIn(f'cd /d "{self.project}"', content)
        self.assertIn(f'start "" "{self.python}" -m trading_bot.paper_app resume', content)
        self.assertIn(f'--project-dir "{self.project}" --runtime-dir "{self.runtime}"', content)
        self.assertNotIn("--submit", content)
        self.assertNotIn("ALPACA", content)
        self.assertNotIn(".env", content)
        self.assertEqual(list(self.entry.parent.iterdir()), [self.entry])
        self.assertFalse(self.runtime.exists(), "Configuring logon must not create or migrate the runtime.")

    def test_owned_entry_can_be_updated_and_removed_without_touching_neighbors(self):
        set_startup(True, self.project, self.runtime)
        neighbor = self.entry.parent / "AnotherApplication.cmd"
        neighbor.write_bytes(b"another application")
        other_runtime = self.root / "another local paper directory"
        set_startup(True, self.project, other_runtime)
        self.assertIn(str(other_runtime), self.entry.read_text(encoding="utf-8"))
        set_startup(False, self.project, other_runtime)
        self.assertFalse(startup_enabled())
        self.assertFalse(self.entry.exists())
        self.assertEqual(neighbor.read_bytes(), b"another application")

    def test_unowned_entry_is_not_overwritten_or_deleted(self):
        self.entry.parent.mkdir(parents=True)
        for content in (b"@echo another application\r\n", b"", b"\xff\xfeinvalid utf8"):
            with self.subTest(content=content):
                self.entry.write_bytes(content)
                self.assertFalse(startup_enabled())
                for enabled in (True, False):
                    with self.assertRaisesRegex(RuntimeError, "not owned"):
                        set_startup(enabled, self.project, self.runtime)
                    self.assertEqual(self.entry.read_bytes(), content)

    def test_directory_at_entry_path_is_not_changed(self):
        self.entry.mkdir(parents=True)
        for enabled in (True, False):
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                set_startup(enabled, self.project, self.runtime)
        self.assertTrue(self.entry.is_dir())

    def test_unsafe_project_or_runtime_paths_are_rejected_before_writing(self):
        for character in ('"', "%", "!", "^", "\r", "\n", "<", ">", "|"):
            for parameter in ("project", "runtime"):
                with self.subTest(character=repr(character), parameter=parameter):
                    unsafe = self.root / f"unsafe{character}directory"
                    with self.assertRaisesRegex(RuntimeError, "absolute path"):
                        set_startup(True, unsafe if parameter == "project" else self.project,
                                    unsafe if parameter == "runtime" else self.runtime)
                    self.assertFalse(self.entry.exists())
        with self.assertRaisesRegex(RuntimeError, "absolute path"):
            set_startup(True, Path("relative-project"), self.runtime)
        with self.assertRaisesRegex(RuntimeError, "absolute path"):
            set_startup(True, self.project, Path("relative-runtime"))

    def test_missing_interpreter_or_project_does_not_install_entry(self):
        self.python.unlink()
        with self.assertRaisesRegex(RuntimeError, "pythonw.exe is missing"):
            set_startup(True, self.project, self.runtime)
        with self.assertRaisesRegex(RuntimeError, "Project directory is missing"):
            set_startup(True, self.root / "missing-project", self.runtime)
        self.assertFalse(self.entry.exists())

    def test_failed_atomic_replace_keeps_existing_owned_entry(self):
        set_startup(True, self.project, self.runtime)
        previous = self.entry.read_bytes()
        with patch.object(startup, "replace_file", side_effect=PermissionError("entry busy")):
            with self.assertRaises(PermissionError):
                set_startup(True, self.project, self.root / "another-runtime")
        self.assertEqual(self.entry.read_bytes(), previous)
        self.assertEqual(list(self.entry.parent.glob(".stocktrading-startup-*.tmp")), [])

    def test_file_created_while_writing_is_not_overwritten(self):
        original_replace = startup.os.fsync

        def create_conflict(handle):
            original_replace(handle)
            self.entry.write_bytes(b"created by another application")

        with patch.object(startup.os, "fsync", side_effect=create_conflict):
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                set_startup(True, self.project, self.runtime)
        self.assertEqual(self.entry.read_bytes(), b"created by another application")

    def test_nonwindows_status_and_disable_are_noops_but_enable_explains(self):
        with patch.object(startup, "_windows", return_value=False):
            self.assertFalse(startup_enabled())
            set_startup(False, self.project, self.runtime)
            with self.assertRaisesRegex(RuntimeError, "only on Windows"):
                set_startup(True, self.project, self.runtime)
        self.assertFalse(self.appdata.exists())

    def test_missing_appdata_is_explicit_and_never_falls_back_into_the_project(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(startup_enabled())
            set_startup(False, self.project, self.runtime)
            with self.assertRaisesRegex(RuntimeError, "APPDATA is unavailable"):
                set_startup(True, self.project, self.runtime)
        self.assertFalse(self.entry.exists())

    def test_enabled_accepts_only_actual_boolean_values(self):
        for enabled in ("true", "false", 0, 1, None):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                set_startup(enabled, self.project, self.runtime)
        self.assertFalse(self.appdata.exists())

    def test_drive_root_argument_doubles_trailing_backslash_when_relevant(self):
        root = Path(self.root.anchor)
        quoted = startup._command_argument(root)
        if str(root).endswith("\\"):
            self.assertEqual(quoted, '"' + str(root) + '\\"')
        else:
            self.assertEqual(quoted, '"' + str(root) + '"')


if __name__ == "__main__":
    unittest.main()
