from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReviewedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="finalizer-source-review-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic Source Reviewer")
        self.git("config", "user.email", "review@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("Synthetic repository\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Synthetic baseline")
        self.path = "credential_transfer.py"
        self.source = self.repo / self.path
        self.source.write_text('def role():\n    return "production"\n')
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text(
            "Reviewed synthetic production-only source. No credentials.\n"
        )
        self.evidence.chmod(0o600)
        self.file = self.base / "review.json"
        now = datetime.now(timezone.utc)
        self.review = {
            "schema_version": 1,
            "kind": "reviewed-sensitive-source",
            "repository": {
                "common_dir": str(self.repo / ".git"),
                "root_commit": self.git("rev-parse", "HEAD"),
            },
            "allocation": {
                "repository_id": "synthetic-repository",
                "allocation_id": "synthetic-allocation",
                "task_key": "source-review",
                "authority_key": "source-review",
                "worktree_path": str(self.repo),
            },
            "scope_sha256": sha((self.path + "\n").encode()),
            "reviewed_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "reviews": [
                {
                    "path": self.path,
                    "sha256": sha(self.source.read_bytes()),
                    "purpose": "Review exact synthetic credential-handling source",
                    "evidence": {
                        "path": str(self.evidence),
                        "sha256": sha(self.evidence.read_bytes()),
                    },
                }
            ],
        }
        self.write_review()

    def git(self, *arguments: str) -> str:
        return (
            subprocess.check_output(
                ["git", "-C", str(self.repo), *arguments],
                env=self.env,
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )

    def write_review(self) -> None:
        self.file.write_text(json.dumps(self.review) + "\n")
        self.file.chmod(0o600)

    def state(self) -> tuple[str, str, str]:
        return (
            self.git("rev-parse", "HEAD"),
            self.git("ls-files", "--stage"),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
        )

    def run_finalizer(
        self,
        mode: str | None = "verify-only",
        *,
        approved: bool = True,
        paths: tuple[str, ...] | None = None,
        extra: tuple[str, ...] = (),
    ) -> tuple[int, dict]:
        before = self.state()
        command = [
            str(ROOT / "codex-git-finalize"),
            "--summary",
            "--repo",
            str(self.repo),
            "--repository-id",
            "synthetic-repository",
            "--allocation-id",
            "synthetic-allocation",
            "--task-key",
            "source-review",
            "--authority-key",
            "source-review",
        ]
        if mode is not None:
            command += ["--mode", mode]
        if mode != "verify-only":
            command += ["--message", "Review synthetic source"]
        if approved:
            command += ["--reviewed-sensitive-source", str(self.file)]
        result = subprocess.run(
            command + list(extra) + ["--", *(paths or (self.path,))],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        if mode is not None:
            self.assertFalse(data["push"]["executed"])
        if mode == "verify-only":
            self.assertEqual(before, self.state())
        return result.returncode, data

    def assert_blocked(self, **kwargs) -> dict:
        for mode in ("verify-only", "commit-only"):
            with self.subTest(mode=mode):
                before = self.state()
                status, data = self.run_finalizer(mode, **kwargs)
                self.assertNotEqual(status, 0, data)
                self.assertEqual(data["status"], "blocked")
                self.assertEqual(before, self.state())
        return data

    def test_exact_review_verifies_and_commits_with_receipt(self) -> None:
        for mode in ("verify-only", "commit-only"):
            status, data = self.run_finalizer(mode)
            self.assertEqual(status, 0, data)
            (receipt,) = data["reviewed_sensitive_sources"]
            self.assertEqual(receipt["sha256"], sha(self.source.read_bytes()))
            self.assertEqual(
                receipt["review_id"], "sha256:" + sha(self.file.read_bytes())
            )
            self.assertEqual(receipt["allocation"], self.review["allocation"])
            self.assertEqual(receipt["content_scan"], "passed")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_missing_review_blocks(self) -> None:
        self.assert_blocked(approved=False)

    def test_normal_publish_keeps_review_and_remote_verification(self) -> None:
        remote = self.base / "remote.git"
        self.git("init", "-q", "--bare", "--initial-branch=main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-qu", "origin", "main")
        status, data = self.run_finalizer(None)
        self.assertEqual(status, 0, data)
        self.assertTrue(data["push"]["executed"])
        self.assertEqual(
            self.git("rev-parse", "HEAD"),
            self.git("--git-dir=" + str(remote), "rev-parse", "main"),
        )
        self.assertEqual(
            data["reviewed_sensitive_sources"][0]["content_scan"], "passed"
        )

    def test_same_path_modified_blocks(self) -> None:
        self.source.write_text(self.source.read_text() + "# changed\n")
        self.assert_blocked()

    def test_different_path_blocks(self) -> None:
        other = "credential_other.py"
        (self.repo / other).write_bytes(self.source.read_bytes())
        self.review["scope_sha256"] = sha((other + "\n").encode())
        self.write_review()
        self.assert_blocked(paths=(other,))

    def test_different_repository_blocks(self) -> None:
        self.review["repository"]["common_dir"] = str(self.base / "other.git")
        self.write_review()
        self.assert_blocked()

    def test_repository_root_mismatch_blocks(self) -> None:
        self.review["repository"]["root_commit"] = "0" * 40
        self.write_review()
        self.assert_blocked()

    def test_scope_and_allocation_mismatch_block(self) -> None:
        self.assert_blocked(paths=(self.path, "README.md"))
        self.review["allocation"]["allocation_id"] = "other-allocation"
        self.write_review()
        self.assert_blocked()

    def test_missing_and_changed_evidence_block(self) -> None:
        self.evidence.unlink()
        self.assert_blocked()
        self.evidence.write_text("Different review\n")
        self.assert_blocked()

    def test_missing_purpose_blocks(self) -> None:
        self.review["reviews"][0]["purpose"] = ""
        self.write_review()
        self.assert_blocked()

    def test_expired_and_overlong_review_block(self) -> None:
        now = datetime.now(timezone.utc)
        self.review["reviewed_at"] = (now - timedelta(days=2)).isoformat()
        self.review["expires_at"] = (now - timedelta(days=1)).isoformat()
        self.write_review()
        self.assert_blocked()
        self.review["expires_at"] = (now + timedelta(days=7)).isoformat()
        self.write_review()
        self.assert_blocked()

    def test_all_secret_detectors_remain_mandatory(self) -> None:
        examples = [
            "gh" + "p_" + "X" * 32,
            "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5,
            "ssh-" + "rsa " + "X" * 48,
            "password" + ' = "synthetic-blocked-value"',
        ]
        for content in examples:
            with self.subTest(detector=examples.index(content)):
                self.source.write_text("# " + content + "\n")
                self.review["reviews"][0]["sha256"] = sha(self.source.read_bytes())
                self.write_review()
                data = self.assert_blocked()
                self.assertNotIn(content, json.dumps(data))
                self.assertNotEqual(
                    data["reviewed_sensitive_sources"][0]["content_scan"], "passed"
                )

    def test_content_exceptions_cannot_combine(self) -> None:
        self.assert_blocked(extra=("--allow-test-fixture", self.path))

    def test_repository_authored_and_writable_review_block(self) -> None:
        self.file.chmod(0o666)
        self.assert_blocked()
        self.file = self.repo / "review.json"
        self.write_review()
        self.assert_blocked()

    def test_symlink_and_invalid_python_block(self) -> None:
        body = self.source.read_bytes()
        self.source.unlink()
        self.source.symlink_to(self.evidence)
        self.assert_blocked()
        self.source.unlink()
        self.source.write_bytes(body + b"invalid python !!!\n")
        self.review["reviews"][0]["sha256"] = sha(self.source.read_bytes())
        self.write_review()
        self.assert_blocked()

    def test_git_clean_filter_hash_change_blocks_before_commit(self) -> None:
        attributes = self.repo / ".git/info/attributes"
        attributes.write_text(self.path + " filter=synthetic\n")
        self.git("config", "filter.synthetic.clean", "sed s/production/changed/")
        before = self.git("rev-parse", "HEAD")
        status, data = self.run_finalizer("commit-only")
        self.assertNotEqual(status, 0, data)
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(before, self.git("rev-parse", "HEAD"))

    def test_commit_hook_change_cannot_reuse_review(self) -> None:
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text(
            "#!/bin/sh\nprintf '# hook changed\\n' >> "
            + self.path
            + "\ngit add -- "
            + self.path
            + "\n"
        )
        hook.chmod(0o700)
        status, data = self.run_finalizer("commit-only")
        self.assertNotEqual(status, 0, data)
        self.assertTrue(data["commit"]["created"])
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.git("rev-parse", "HEAD"), data["commit"]["sha"])


if __name__ == "__main__":
    unittest.main()
