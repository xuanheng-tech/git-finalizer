from __future__ import annotations

from contextlib import suppress
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from git_finalizer import git_io
from git_finalizer.delivery import content_scan, runtime
from git_finalizer.toolchain import tool_skill_sync as sync


class RuntimeGuardTests(unittest.TestCase):
    def test_named_summary_state_is_order_independent_and_preserves_values(self) -> None:
        state = runtime.SummaryState(
            repository="/synthetic/等号=与换行\nvalue",
            mode="verify-only",
            status="blocked",
            final_phase="cli",
            exit_code=1,
            requested_path_count=2,
            warnings=["first=warning", "second"],
            reason="reason=value\nsecond line",
        )
        left = state.to_dict()
        right = runtime.SummaryState(
            reason="reason=value\nsecond line",
            warnings=["first=warning", "second"],
            requested_path_count=2,
            exit_code=1,
            final_phase="cli",
            status="blocked",
            mode="verify-only",
            repository="/synthetic/等号=与换行\nvalue",
        ).to_dict()
        self.assertEqual(left, right)
        self.assertEqual(left["repository"], "/synthetic/等号=与换行\nvalue")
        self.assertEqual(left["reason"], "reason=value second line")
        self.assertEqual(left["warnings"], ["first=warning", "second"])
        self.assertTrue(all(k in left for k in runtime.FIXED_SUMMARY_KEYS))

    def test_scan_budget_blocks_before_reading_a_large_blob(self) -> None:
        with (
            patch.object(content_scan, "MAX_SCAN_BYTES", 128),
            patch.object(
                git_io,
                "run_git",
                return_value=subprocess.CompletedProcess(["git", "cat-file", "-s"], 0, b"256\n", b""),
            ),
            self.assertRaisesRegex(ValueError, "byte budget"),
        ):
            git_io.read_blob_bytes(Path("/synthetic"), "a" * 40, max_bytes=content_scan.MAX_SCAN_BYTES)
        with (
            patch.object(content_scan, "MAX_SCAN_BYTES", 128),
            self.assertRaisesRegex(ValueError, "byte budget"),
        ):
            content_scan.scan(b"x" * 256)

    def test_file_budget_failure_is_a_redacted_scanner_error(self) -> None:
        with patch.object(content_scan, "MAX_SCAN_BYTES", 128):
            diag = content_scan.scan_diagnostic(b"synthetic-large-content" * 20, path="example.txt", stage="worktree")
        self.assertIsNotNone(diag)
        assert diag is not None
        self.assertEqual(diag["verdict"], "ERROR")
        self.assertEqual(diag["detector"], "content-scan-error")

    def test_installed_invocation_waits_for_the_complete_production_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary)
            shutil.copy2(ROOT / "git-finalize", binary / "git-finalize")
            shutil.copytree(ROOT / "src", binary / "src")
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
            entry.write_text("#!/bin/sh\nexit 0\n")
            with sync.finalizer_execution_lock([entry]):
                pass
            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
                "from pathlib import Path\n"
                "from git_finalizer.delivery.runtime import execution_lock\n"
                f"with execution_lock(Path({str(entry)!r})):\n"
                "    print('ready', flush=True)\n"
                "    sys.stdin.readline()\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline(), b"ready\n")
                with (
                    entry.with_name(".git-finalizer-execution.lock").open("rb") as lock,
                    self.assertRaises(BlockingIOError),
                ):
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
            with (
                self.assertRaises(OSError),
                sync.finalizer_execution_lock([entry]),
            ):
                self.fail("unsafe lock was accepted")

    def test_summary_shell_keeps_the_lock_after_launcher_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "git-finalize"
            entry.write_text("#!/bin/sh\nexit 0\n")
            with sync.finalizer_execution_lock([entry]):
                pass
            script = (
                "import os, subprocess, sys\n"
                f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
                "from pathlib import Path\n"
                "from git_finalizer.delivery.runtime import execution_lock\n"
                f"with execution_lock(Path({str(entry)!r})):\n"
                "    child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'], close_fds=True)\n"
                "    print(child.pid, flush=True)\n"
                "    sys.stdin.readline()\n"
                "    child.terminate()\n"
                "    child.wait(timeout=5)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            child = None
            try:
                child = int(process.stdout.readline())
                # Verify child subprocess does NOT inherit LOCK_EX FD (P1 FD leak fix!).
                os.kill(child, 0)
                with (
                    entry.with_name(".git-finalizer-execution.lock").open("rb") as lock,
                    self.assertRaises(BlockingIOError),
                ):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.communicate(b"finish\n", timeout=10)
                with entry.with_name(".git-finalizer-execution.lock").open("rb") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                child = None
            finally:
                if process.poll() is None:
                    process.kill()
                if child is not None:
                    with suppress(ProcessLookupError):
                        os.kill(child, signal.SIGTERM)
                process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
