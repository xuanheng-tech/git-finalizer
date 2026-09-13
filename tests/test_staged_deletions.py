from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tool_skill_manifest.json").read_text())
FINALIZER = ROOT / MANIFEST["executable"]["entrypoints"][0]


class StagedDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="finalizer-staged-paths-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = self.root / "home"
        home.mkdir()
        self.env = {
            "PATH": os.defpath,
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }

    def git(self, repo: Path, *args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], env=self.env, stderr=subprocess.PIPE
        )

    def repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "--quiet", "--initial-branch=feature")
        self.git(repo, "config", "user.name", "Synthetic Test Author")
        self.git(repo, "config", "user.email", "synthetic@example.invalid")
        (repo / "old.txt").write_text("original payload\n")
        (repo / "keep.txt").write_text("unrelated payload\n")
        self.git(repo, "add", "--", "old.txt", "keep.txt")
        self.git(repo, "commit", "--quiet", "-m", "fixture base")
        return repo

    def state(self, repo: Path) -> tuple[bytes, bytes, bytes]:
        return (
            self.git(repo, "rev-parse", "HEAD"),
            (repo / ".git/index").read_bytes(),
            self.git(repo, "status", "--porcelain=v1", "-z"),
        )

    def finalize(self, repo: Path, mode: str, *paths: str) -> subprocess.CompletedProcess[str]:
        args = [str(FINALIZER), "--summary", "--mode", mode, "--repo", str(repo)]
        if mode == "commit-only":
            args += ["--message", "fixture change"]
        return subprocess.run(
            [*args, "--", *paths], cwd=repo, env=self.env,
            text=True, capture_output=True, check=False,
        )

    def assert_success(self, repo: Path, mode: str, before: tuple[bytes, bytes, bytes],
                       result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "success")
        self.assertFalse(summary["push"]["executed"])
        if mode == "verify-only":
            self.assertEqual(self.state(repo), before)
            self.assertFalse(summary["commit"]["created"])
        else:
            self.assertTrue(summary["commit"]["created"])
            self.assertNotEqual(self.git(repo, "rev-parse", "HEAD"), before[0])
            self.assertEqual(self.git(repo, "status", "--porcelain=v1"), b"")

    def test_staged_deletion_passes_without_readding_the_absent_path(self) -> None:
        for mode in ("verify-only", "commit-only"):
            with self.subTest(mode=mode):
                repo = self.repo(mode)
                self.git(repo, "rm", "--", "old.txt")
                before = self.state(repo)
                self.assert_success(repo, mode, before, self.finalize(repo, mode, "old.txt"))
                self.assertFalse((repo / "old.txt").exists())

    def test_staged_rename_accepts_both_exact_sides(self) -> None:
        for mode in ("verify-only", "commit-only"):
            with self.subTest(mode=mode):
                repo = self.repo(mode)
                self.git(repo, "mv", "--", "old.txt", "new.txt")
                before = self.state(repo)
                self.assert_success(
                    repo, mode, before, self.finalize(repo, mode, "old.txt", "new.txt")
                )
                self.assertEqual((repo / "new.txt").read_text(), "original payload\n")

    def test_never_tracked_missing_path_is_rejected_without_mutation(self) -> None:
        for mode in ("verify-only", "commit-only"):
            with self.subTest(mode=mode):
                repo = self.repo(mode)
                before = self.state(repo)
                result = self.finalize(repo, mode, "missing.txt")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("tracked deletion", json.loads(result.stdout)["reason"])
                self.assertEqual(self.state(repo), before)

    def test_historical_deletion_does_not_authorize_a_missing_path(self) -> None:
        for mode in ("verify-only", "commit-only"):
            with self.subTest(mode=mode):
                repo = self.repo(mode)
                self.git(repo, "rm", "--", "old.txt")
                self.git(repo, "commit", "--quiet", "-m", "fixture prior deletion")
                before = self.state(repo)
                result = self.finalize(repo, mode, "old.txt")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("tracked deletion", json.loads(result.stdout)["reason"])
                self.assertEqual(self.state(repo), before)

    def test_literal_path_does_not_match_another_staged_deletion(self) -> None:
        repo = self.repo("literal")
        self.git(repo, "rm", "--", "old.txt")
        before = self.state(repo)
        result = self.finalize(repo, "commit-only", "*.txt", "old.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked deletion", json.loads(result.stdout)["reason"])
        self.assertEqual(self.state(repo), before)

    def test_staged_deletion_keeps_out_of_scope_staged_changes_blocked(self) -> None:
        repo = self.repo("scope")
        self.git(repo, "rm", "--", "old.txt")
        (repo / "keep.txt").write_text("changed out of scope\n")
        self.git(repo, "add", "--", "keep.txt")
        before = self.state(repo)
        result = self.finalize(repo, "commit-only", "old.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("keep.txt", json.loads(result.stdout)["reason"])
        self.assertEqual(self.state(repo), before)

    def test_recreated_unignored_file_is_staged_as_new_content(self) -> None:
        repo = self.repo("recreated")
        self.git(repo, "rm", "--", "old.txt")
        (repo / "old.txt").write_text("replacement payload\n")
        before = self.state(repo)
        self.assert_success(
            repo, "commit-only", before, self.finalize(repo, "commit-only", "old.txt")
        )
        self.assertEqual(self.git(repo, "show", "HEAD:old.txt"), b"replacement payload\n")
