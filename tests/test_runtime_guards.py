from __future__ import annotations

import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tooling import tool_skill_sync as sync

ROOT = Path(__file__).resolve().parents[1]


class RuntimeGuardTests(unittest.TestCase):
    def test_named_summary_state_is_order_independent_and_preserves_values(self) -> None:
        runtime = runpy.run_path(str(ROOT / "git-finalize-runtime.py"))
        fields = dict.fromkeys(runtime["SUMMARY_FIELDS"], "")
        fields.update(SUMMARY_SCHEMA_VERSION="1", VERSION="test", summary_path_count="2",
                      summary_exit_code="1", summary_warnings_omitted="0",
                      summary_repo="/synthetic/等号=与换行\n", summary_mode="verify-only",
                      summary_status="blocked", summary_reason="reason=value\nsecond line")
        parse = runtime["summary_arguments"]
        render = runtime["render_summary"]
        arguments = [f"{key}={value}" for key, value in fields.items()]
        left = render(*parse(arguments + ["warning=first=warning", "warning=second"]))
        right = render(*parse(list(reversed(arguments)) + ["warning=first=warning", "warning=second"]))
        self.assertEqual(left, right)
        self.assertEqual(left["repository"], fields["summary_repo"])
        self.assertEqual(left["reason"], fields["summary_reason"])
        self.assertEqual(left["warnings"], ["first=warning", "second"])
        for invalid in (arguments[1:], arguments + [arguments[0]], arguments + ["unknown=value"]):
            with self.assertRaises(ValueError):
                parse(invalid)

    def test_scan_budget_blocks_before_reading_a_large_blob(self) -> None:
        scanner = runpy.run_path(str(ROOT / "git-finalize-content-scan.py"))
        namespace = scanner["read_blob"].__globals__
        with patch.dict(namespace, MAX_SCAN_BYTES=128), patch.dict(
            namespace, git=lambda *args, **kwargs: b"256\n"
        ):
            with self.assertRaisesRegex(ValueError, "byte budget"):
                scanner["read_blob"]("/synthetic", "a" * 40)
        with patch.dict(scanner["scan"].__globals__, MAX_SCAN_BYTES=128):
            with self.assertRaisesRegex(ValueError, "byte budget"):
                scanner["scan"](b"x" * 256)

    def test_file_budget_failure_is_a_redacted_scanner_error(self) -> None:
        scanner = runpy.run_path(str(ROOT / "git-finalize-content-scan.py"))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "example.txt"
            source.write_bytes(b"synthetic-large-content" * 20)
            output = io.StringIO()
            with patch.dict(scanner["main"].__globals__, MAX_SCAN_BYTES=128), patch.object(
                sys, "argv", ["scanner", "report-file", str(source), temporary, "", ""]
            ), redirect_stdout(output):
                self.assertEqual(scanner["main"](), 1)
            self.assertEqual(output.getvalue(), "ERROR\n")

    def test_installed_invocation_waits_for_the_complete_production_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary)
            for source in ROOT.glob("git-finalize*"):
                if source.is_file():
                    shutil.copy2(source, binary / source.name)
            entry = binary / "git-finalize"
            with sync.finalizer_execution_lock([entry]):
                process = subprocess.Popen(
                    [str(entry), "--summary", "--mode", "unsupported", "--repo", str(binary)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=0.2)
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
            output, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 1, errors)
            self.assertEqual(json.loads(output)["final_phase"], "cli")
            self.assertEqual(list(binary.glob("__pycache__")), [])

    def test_running_invocation_holds_the_execution_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "git-finalize"
            entry.write_text("#!/bin/bash\nprintf 'ready\\n'\nread -r line\n")
            with sync.finalizer_execution_lock([entry]):
                pass
            process = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "git-finalize-runtime.py"),
                 "locked-entrypoint", str(entry)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline(), b"ready\n")
                with entry.with_name(".git-finalizer-execution.lock").open("rb") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    output, errors = process.communicate(b"finish\n", timeout=10)
                    self.assertEqual(process.returncode, 0, errors)
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_execution_lock_refuses_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "git-finalize"
            entry.with_name(".git-finalizer-execution.lock").symlink_to("other")
            with self.assertRaises(OSError):
                with sync.finalizer_execution_lock([entry]):
                    self.fail("unsafe lock was accepted")


if __name__ == "__main__":
    unittest.main()
