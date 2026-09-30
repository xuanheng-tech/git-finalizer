from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


FINALIZER = Path(__file__).resolve().parents[1] / "git-finalize"


class ResolvedMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gf-resolved-merge-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.env = {
            "PATH": os.defpath, "HOME": str(self.repo), "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "TMPDIR": str(self.repo),
        }
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic Merge Reviewer")
        self.git("config", "user.email", "merge@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit("base\n")
        self.git("checkout", "-qb", "side")
        self.parent = self.commit("side\n")
        self.git("checkout", "-q", "main")
        self.head = self.commit("main\n")
        merge = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-ff", "--no-commit", "side"],
            env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(merge.returncode, 1)
        (self.repo / "f.txt").write_text("reviewed resolution\n", encoding="utf-8")
        self.git("add", "--", "f.txt")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], env=self.env,
            stderr=subprocess.PIPE, timeout=10,
        ).decode().strip()

    def commit(self, content: str) -> str:
        (self.repo / "f.txt").write_text(content, encoding="utf-8")
        self.git("add", "--", "f.txt")
        self.git("commit", "-qm", "synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def state(self) -> tuple[str, str, str, bytes | None]:
        marker = self.repo / ".git" / "MERGE_HEAD"
        return (
            self.git("rev-parse", "HEAD"), self.git("ls-files", "--stage"),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
            marker.read_bytes() if marker.exists() else None,
        )

    def finalize(self, mode: str = "verify-only", *, flags: list[str] | None = None) -> dict:
        args = [str(FINALIZER), "--summary", "--repo", str(self.repo)]
        if mode: args += ["--mode", mode]
        args += flags if flags is not None else ["--merge-parent", self.parent]
        if mode != "verify-only": args += ["--message", "Complete reviewed merge"]
        result = subprocess.run(
            [*args, "--", "f.txt"], env=self.env, capture_output=True,
            text=True, timeout=20,
        )
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode == 0, data["status"] == "success", data)
        self.assertFalse(data["push"]["executed"], data)
        return data

    def blocked_unchanged(self, *, flags: list[str] | None = None, mode: str = "verify-only") -> None:
        before = self.state()
        data = self.finalize(mode, flags=flags)
        self.assertEqual(data["status"], "blocked", data)
        self.assertFalse(data["commit"]["created"], data)
        self.assertEqual(self.state(), before)

    def test_verification_preserves_head_index_worktree_and_merge_identity(self) -> None:
        before = self.state()
        data = self.finalize()
        self.assertEqual(data["status"], "success", data)
        self.assertEqual(data["mode_result"]["local_validation"], "passed")
        self.assertEqual(self.state(), before)

    def test_local_commit_has_exact_two_parents_and_reviewed_tree(self) -> None:
        tree = self.git("write-tree")
        data = self.finalize("commit-only")
        self.assertEqual(data["status"], "success", data)
        oid = data["commit"]["sha"]
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", oid), f"{oid} {self.head} {self.parent}")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), tree)
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_pending_merge_is_still_refused_without_explicit_parent(self) -> None:
        self.blocked_unchanged(flags=[])

    def test_parent_must_match_exactly_and_only_once(self) -> None:
        for flags in (["--merge-parent", self.base], ["--merge-parent", self.parent[:12]],
                      ["--merge-parent", self.parent, "--merge-parent", self.parent]):
            with self.subTest(flags=flags): self.blocked_unchanged(flags=flags)

    def test_direct_publication_is_never_accepted(self) -> None:
        self.blocked_unchanged(mode="")

    def test_local_mode_cannot_hide_a_publication_selector(self) -> None:
        for selector in ("--resume-publish", "--publish-existing-branch", "--sync-published-branch"):
            with self.subTest(selector=selector):
                self.blocked_unchanged(flags=["--merge-parent", self.parent, selector, self.head])

    def test_local_dry_run_preserves_the_resolved_merge(self) -> None:
        before = self.state()
        data = self.finalize("commit-only", flags=["--merge-parent", self.parent, "--dry-run"])
        self.assertEqual(data["status"], "success", data)
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(self.state(), before)

    def test_missing_merge_and_multiple_merge_parents_are_refused(self) -> None:
        marker = self.repo / ".git" / "MERGE_HEAD"
        marker.unlink()
        self.blocked_unchanged()
        for content in (f"{self.parent}\n{self.base}\n", f"{self.parent}\n\n"):
            marker.write_text(content)
            self.blocked_unchanged()

    def test_symlinked_merge_identity_is_refused(self) -> None:
        marker = self.repo / ".git" / "MERGE_HEAD"
        alternate = self.repo / ".git" / "synthetic-parent"
        marker.rename(alternate)
        marker.symlink_to(alternate)
        self.blocked_unchanged()

    def test_merge_identity_requires_exact_bytes(self) -> None:
        marker = self.repo / ".git" / "MERGE_HEAD"
        for content in (self.parent.encode(), self.parent.encode() + b"\x00\n",
                        self.parent.encode() + b"\n" + b"X" * 65536):
            with self.subTest(length=len(content)):
                marker.write_bytes(content)
                self.blocked_unchanged()

    def test_other_incomplete_operations_remain_refused(self) -> None:
        for name in ("CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-apply", "rebase-merge", "sequencer", "BISECT_START"):
            with self.subTest(name=name):
                marker = self.repo / ".git" / name
                marker.write_text(self.base + "\n")
                self.blocked_unchanged()
                marker.unlink()

    def test_unresolved_index_is_refused(self) -> None:
        blob = self.git("rev-parse", f"{self.base}:f.txt")
        subprocess.run(
            ["git", "-C", str(self.repo), "update-index", "--index-info"],
            input=f"0 {'0' * 40}\tf.txt\n100644 {blob} 1\tf.txt\n",
            text=True, env=self.env, check=True, timeout=10,
        )
        self.blocked_unchanged()

    def test_staged_changes_outside_scope_remain_refused(self) -> None:
        (self.repo / "other.txt").write_text("outside explicit scope\n")
        self.git("add", "--", "other.txt")
        self.blocked_unchanged()

    def test_content_scanning_remains_mandatory(self) -> None:
        (self.repo / "f.txt").write_text("-----BEGIN " + "PRIVATE KEY-----\nSYNTHETIC TEST BODY\n")
        self.git("add", "--", "f.txt")
        self.blocked_unchanged()

    def test_hook_parent_drift_preserves_created_commit_without_publication(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\nprintf '%s\\n' '{self.base}' > .git/MERGE_HEAD\n")
        hook.chmod(0o700)
        data = self.finalize("commit-only")
        self.assertEqual(data["status"], "failed", data)
        self.assertTrue(data["commit"]["created"], data)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.head)


if __name__ == "__main__":
    unittest.main()
