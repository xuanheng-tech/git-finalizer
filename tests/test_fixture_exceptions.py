from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODES = ("--publish-existing-history", "--resume-existing-history-publish")
SYNTHETIC = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5 + "\nSYNTHETIC TEST BODY\n"


class FixtureExceptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="finalizer-fixture-exception-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.remote = self.base / "remote.git"
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.repo.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic Fixture Reviewer")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("init", "-q", "--bare", "--initial-branch=main", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "README.md").write_text("Synthetic repository\n")
        self.commit()
        self.root_oid = self.git("rev-parse", "HEAD")
        (self.repo / "tests").mkdir()
        self.path = "tests/example.py"
        (self.repo / self.path).write_text(SYNTHETIC)
        self.commit()
        self.approval = {
            "schema_version": 1,
            "repository": {
                "root_commit": self.root_oid,
                "remote_url_sha256": hashlib.sha256(
                    str(self.remote).encode()
                ).hexdigest(),
            },
            "exceptions": [
                {
                    "path": self.path,
                    "blob_oid": self.git("rev-parse", "HEAD:" + self.path),
                    "sha256": hashlib.sha256(SYNTHETIC.encode()).hexdigest(),
                    "detector": "private-key-header-v1",
                    "reason": "Locally constructed synthetic boundary and non-key body",
                }
            ],
        }
        self.file = self.base / "approval.json"
        self.write_approval()

    def git(self, *args: str) -> str:
        return (
            subprocess.check_output(
                ["git", "-C", str(self.repo), *args],
                env=self.env,
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )

    def commit(self) -> None:
        self.git("add", "--all")
        self.git("commit", "-qm", "Synthetic fixture change")

    def write_approval(self) -> None:
        self.file.write_text(json.dumps(self.approval) + "\n")

    def run_finalizer(self, mode: str, *, approved: bool = True) -> tuple[int, dict]:
        before = (
            self.git("rev-parse", "HEAD"),
            self.git("ls-files", "--stage"),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
        )
        command = [
            str(ROOT / "codex-git-finalize"),
            "--summary",
            mode,
            before[0],
            "--remote",
            "origin",
            "--remote-branch",
            "main",
            "--repo",
            str(self.repo),
        ]
        if approved:
            command += ["--fixture-exceptions", str(self.file)]
        result = subprocess.run(
            command,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        data = json.loads(result.stdout)
        after = (
            self.git("rev-parse", "HEAD"),
            self.git("ls-files", "--stage"),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
        )
        self.assertEqual(before, after)
        self.assertNotIn("SYNTHETIC TEST BODY", result.stdout + result.stderr)
        return result.returncode, data

    def assert_blocked_both(self, *, approved: bool = True) -> None:
        refs = (
            self.git("--git-dir=" + str(self.remote), "show-ref")
            if (self.remote / "refs/heads/main").exists()
            else ""
        )
        for mode in MODES:
            with self.subTest(mode=mode):
                status, data = self.run_finalizer(mode, approved=approved)
                self.assertNotEqual(status, 0)
                self.assertEqual(data["status"], "blocked")
                self.assertFalse(data["push"]["executed"])
                self.assertEqual(
                    refs,
                    self.git(
                        "--git-dir=" + str(self.remote),
                        "for-each-ref",
                        "--format=%(objectname) %(refname)",
                    ),
                )

    def assert_exact_pass(self, mode: str) -> dict:
        status, data = self.run_finalizer(mode)
        self.assertEqual(status, 0, data)
        (receipt,) = data["fixture_exceptions"]
        approval = {
            "schema_version": 1,
            "repository": self.approval["repository"],
            "exception": self.approval["exceptions"][0],
        }
        expected_id = hashlib.sha256(
            json.dumps(approval, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(
            receipt,
            {
                "exception_id": "sha256:" + expected_id,
                "repository": self.approval["repository"],
                **self.approval["exceptions"][0],
                "applied": True,
            },
        )
        self.assertEqual(data["mode_result"]["post_verify"], "passed")
        self.assertEqual(
            self.git("rev-parse", "HEAD"),
            self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/main"),
        )
        return receipt

    def test_exact_initial_and_already_pushed_resume_preserve_receipt(self) -> None:
        first = self.assert_exact_pass(MODES[0])
        self.assertEqual(first, self.assert_exact_pass(MODES[1]))
        # Even an already-published result cannot reuse an approval implicitly.
        status, data = self.run_finalizer(MODES[1], approved=False)
        self.assertNotEqual(status, 0)
        self.assertFalse(data["push"]["executed"])

    def test_exact_resume_from_empty_remote(self) -> None:
        self.assert_exact_pass(MODES[1])

    def test_no_exception_and_one_byte_mutation_block_both(self) -> None:
        self.assert_blocked_both(approved=False)
        with (self.repo / self.path).open("a") as stream:
            stream.write("X")
        self.commit()
        self.assert_blocked_both()

    def test_same_fixture_at_another_path_blocks_both(self) -> None:
        (self.repo / "tests/other.py").write_text(SYNTHETIC)
        self.commit()
        self.assert_blocked_both()

    def test_one_byte_mutation_removing_detector_signature_still_blocks(self) -> None:
        (self.repo / self.path).write_text("+" + SYNTHETIC[1:])
        self.commit()
        self.assert_blocked_both()

    def test_unrelated_private_key_shaped_content_blocks_both(self) -> None:
        (self.repo / "tests/unrelated.py").write_text(
            SYNTHETIC + "DIFFERENT SYNTHETIC BODY\n"
        )
        self.commit()
        self.assert_blocked_both()

    def test_other_detector_still_blocks_the_approved_blob(self) -> None:
        content = SYNTHETIC + "ghp_" + "SYNTHETICONLY" * 3 + "\n"
        (self.repo / self.path).write_text(content)
        self.commit()
        additional = dict(
            self.approval["exceptions"][0],
            blob_oid=self.git("rev-parse", "HEAD:" + self.path),
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
        self.approval["exceptions"].append(additional)
        self.write_approval()
        self.assert_blocked_both()

    def test_multiple_rules_need_independent_exact_approvals(self) -> None:
        content = (
            SYNTHETIC
            + "AKIA"
            + "ABCDEFGHIJKLMNOP\n"
            + "secret"
            + " = '"
            + "SYNTHETIC_ASSIGNMENT_VALUE"
            + "'\n"
        )
        (self.repo / self.path).write_text(content)
        self.commit()
        entry = dict(
            self.approval["exceptions"][0],
            blob_oid=self.git("rev-parse", "HEAD:" + self.path),
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
        self.approval["exceptions"].extend(
            dict(entry, detector=rule)
            for rule in ("private-key-header-v1", "known-token-v1")
        )
        self.write_approval()
        self.assert_blocked_both()
        self.approval["exceptions"].append(
            dict(entry, detector="credential-assignment-v1")
        )
        self.write_approval()
        receipts = None
        for mode in MODES:
            status, data = self.run_finalizer(mode)
            self.assertEqual(status, 0, data)
            self.assertEqual(len(data["fixture_exceptions"]), 4)
            self.assertTrue(all(item["applied"] for item in data["fixture_exceptions"]))
            if receipts is not None:
                self.assertEqual(receipts, data["fixture_exceptions"])
            receipts = data["fixture_exceptions"]

    def test_invalid_approvals_fail_closed_in_both_modes(self) -> None:
        original = copy.deepcopy(self.approval)
        changes = (
            ("repository", "root_commit", "f" * 40),
            ("repository", "remote_url_sha256", "f" * 64),
            ("exception", "sha256", "f" * 64),
            ("exception", "blob_oid", "f" * 40),
            ("exception", "detector", "unknown-detector"),
            ("exception", "unknown", True),
            ("exception", "reason", ""),
            ("exception", "path", "tests/*.py"),
            ("exception", "path", "tests/../example.py"),
        )
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                self.approval = copy.deepcopy(original)
                target = (
                    self.approval["repository"]
                    if section == "repository"
                    else self.approval["exceptions"][0]
                )
                target[key] = value
                self.write_approval()
                self.assert_blocked_both()
        for raw in (
            "{",
            '{"schema_version":1,"schema_version":1}',
            json.dumps(dict(original, schema_version=2)),
            json.dumps(dict(original, exceptions=original["exceptions"] * 2)),
        ):
            with self.subTest(raw=raw):
                self.file.write_text(raw)
                self.assert_blocked_both()


if __name__ == "__main__":
    unittest.main()
