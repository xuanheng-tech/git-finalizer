from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeliveryInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gf-delivery-invariants-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        self.git("init", "-q", "--initial-branch=feature", str(self.repo))
        self.git("config", "user.name", "Synthetic Author")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.repo / "wanted.txt").write_text("base\n")
        self.git("add", "--", "wanted.txt")
        self.git("commit", "-qm", "base")
        self.git("init", "-q", "--bare", "--initial-branch=feature", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "--set-upstream", "origin", "feature")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "wanted.txt").write_text("reviewed candidate\n")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo if self.repo.exists() else self.root), *arguments],
            env=self.environment, stderr=subprocess.PIPE, text=True,
        )

    def finalize(self, mode: str = "normal") -> tuple[subprocess.CompletedProcess[str], dict]:
        command = [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo)]
        if mode != "normal":
            command.extend(["--mode", mode])
        if mode != "verify-only":
            command.extend(["--message", "reviewed change"])
        result = subprocess.run(
            [*command, "--", "wanted.txt"], env=self.environment,
            capture_output=True, text=True, timeout=30,
        )
        return result, json.loads(result.stdout)

    def remote_head(self) -> str:
        return self.git("ls-remote", "--refs", "origin", "refs/heads/feature").split()[0]

    def test_hook_cannot_replace_reviewed_content_in_the_same_path(self) -> None:
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'unreviewed hook content\\n' > wanted.txt\ngit add -- wanted.txt\n")
        hook.chmod(0o755)
        result, summary = self.finalize()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.remote_head(), self.base)
        self.assertIn("tree", summary["reason"])

    def test_pending_operations_never_turn_into_a_commit(self) -> None:
        marker = self.repo / ".git/CHERRY_PICK_HEAD"
        marker.write_text(self.base + "\n")
        before = (self.repo / ".git/index").read_bytes()
        for mode in ("verify-only", "commit-only", "normal"):
            with self.subTest(mode=mode):
                result, summary = self.finalize(mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(summary["commit"]["created"])
                self.assertFalse(summary["push"]["executed"])
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
                self.assertEqual((self.repo / ".git/index").read_bytes(), before)
        self.assertEqual(marker.read_text(), self.base + "\n")

    def test_failed_commit_process_reports_the_commit_it_already_created(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        wrapper = binary / "git"
        wrapper.write_text(
            '#!/bin/sh\nis_commit=0\nfor arg in "$@"; do\n'
            '  [ "$arg" = commit ] && is_commit=1\ndone\n'
            '/usr/bin/git "$@"\nresult=$?\n'
            '[ "$is_commit" = 1 ] && [ "$result" = 0 ] && exit 1\nexit "$result"\n'
        )
        wrapper.chmod(0o755)
        self.environment["PATH"] = str(binary) + os.pathsep + os.defpath
        result, summary = self.finalize()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(self.remote_head(), self.base)

    def test_normal_publish_checks_existing_unpublished_commits(self) -> None:
        # Synthetic data only; the path itself exercises the sensitive-path gate.
        (self.repo / ".env").write_text("SYNTHETIC_FIXTURE=example\n")
        self.git("add", "--", ".env")
        self.git("commit", "-qm", "unreviewed local history")
        unpublished = self.git("rev-parse", "HEAD").strip()
        before_index = (self.repo / ".git/index").read_bytes()
        result, summary = self.finalize()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.remote_head(), self.base)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), unpublished)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)

    def test_summary_uses_the_python_selected_from_path(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        probe = binary / "python3"
        log = self.root / "python-probe.log"
        probe.write_text(f'#!/bin/sh\nprintf "selected\\n" >> "{log}"\nexec "{sys.executable}" "$@"\n')
        probe.chmod(0o755)
        self.environment["PATH"] = str(binary) + os.pathsep + os.defpath
        result, summary = self.finalize("verify-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["status"], "success")
        self.assertTrue(log.exists(), "the CLI bypassed the caller's Python selection")

    def test_initial_branch_publish_checks_inherited_local_history(self) -> None:
        self.git("config", "--unset", "branch.feature.remote")
        self.git("config", "--unset", "branch.feature.merge")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.git("-C", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        self.git("push", "-q", "origin", ":refs/heads/feature")
        (self.repo / ".env").write_text("SYNTHETIC_FIXTURE=example\n")
        self.git("add", "--", ".env")
        self.git("commit", "-qm", "unreviewed local history")
        before = self.git("rev-parse", "HEAD").strip()
        result = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", "--initial-branch-publish",
             "--remote", "origin", "--remote-branch", "feature", "--repo", str(self.repo),
             "--message", "reviewed change", "--", "wanted.txt"],
            env=self.environment, capture_output=True, text=True, timeout=30,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertEqual(self.git("ls-remote", "--refs", "origin", "refs/heads/feature"), "")

    def test_quoted_json_credential_assignment_is_checked(self) -> None:
        (self.repo / "wanted.txt").write_text(json.dumps({"password": "synthetic-fixture-value"}))
        result, summary = self.finalize("verify-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(summary["commit"]["created"])
        self.assertIn("credential", summary["reason"])


if __name__ == "__main__":
    unittest.main()
