"""Exercise the Windows launcher without importing the trading CLI or broker."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


POWERSHELL = shutil.which("powershell.exe")
LAUNCHER = Path(__file__).resolve().parents[1] / "Start-PaperCandidate.ps1"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class CandidateLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # Point a disposable copy to this test interpreter. Everything else in
        # the production launcher runs unchanged, including native stderr pipes.
        source = LAUNCHER.read_text(encoding="utf-8")
        interpreter_line = "$candidatePython = Join-Path $PSScriptRoot '.venv\\Scripts\\python.exe'"
        self.assertIn(interpreter_line, source)
        interpreter_path = str(Path(sys.executable).resolve()).replace("'", "''")
        self.script = self.root / LAUNCHER.name
        self.script.write_text(
            source.replace(interpreter_line, f"$candidatePython = '{interpreter_path}'"),
            encoding="utf-8",
        )
        package = self.root / "trading_bot"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.stub = package / "cli.py"
        # Even if a ShowCommand test regresses, fail locally with no broker call.
        self.stub.write_text("raise RuntimeError('offline CLI stub invoked')\n", encoding="utf-8")

    def run_launcher(self, *arguments):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), *arguments],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def command(self, *arguments):
        result = self.run_launcher(*arguments, "-ShowCommand")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "var").exists(), "ShowCommand must not create runtime state")
        return result.stdout

    def test_preview_is_one_time_without_submission(self):
        command = self.command()
        self.assertIn("alpaca-once", command)
        self.assertIn("--journal-dir var\\relative_strength_preview", command)
        self.assertNotIn("--submit", command)
        self.assertNotIn("--continuous", command)

    def test_default_submission_ends_at_close(self):
        command = self.command("-Submit")
        self.assertIn("alpaca-session", command)
        self.assertIn("--journal-dir var --submit", command)
        self.assertIn("--until-close", command)
        self.assertIn("--wait-for-open", command)
        self.assertNotIn("--continuous", command)

    def test_continuous_submission_uses_exclusive_session_mode(self):
        command = self.command("-Submit", "-Continuous")
        self.assertIn("alpaca-session", command)
        self.assertIn("--journal-dir var --submit", command)
        self.assertIn("--continuous", command)
        self.assertNotIn("--until-close", command)
        self.assertNotIn("--wait-for-open", command)

    def test_continuous_requires_explicit_submission(self):
        result = self.run_launcher("-Continuous", "-ShowCommand")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-Continuous requires -Submit", result.stdout + result.stderr)
        self.assertFalse((self.root / "var").exists())

    def test_native_failure_retains_complete_stderr_log_and_exit_code(self):
        self.stub.write_text(
            "import sys\n"
            "print('offline standard output', flush=True)\n"
            "print('offline stderr first line', file=sys.stderr, flush=True)\n"
            "print('offline stderr final line', file=sys.stderr, flush=True)\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        result = self.run_launcher("-Submit", "-Continuous")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exit code 7", output)
        self.assertIn("waits through nights and market holidays", output)
        logs = list((self.root / "var" / "logs").glob("candidate-*.log"))
        self.assertEqual(len(logs), 1)
        # Tee-Object in Windows PowerShell 5.1 writes UTF-16 with a BOM.
        log = logs[0].read_text(encoding="utf-16")
        self.assertIn("offline standard output", log)
        self.assertIn("offline stderr first line", log)
        self.assertIn("offline stderr final line", log)


if __name__ == "__main__":
    unittest.main()
