"""Content already in the committed baseline is not re-reported; new matches still are."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / json.loads((ROOT / "tool_skill_manifest.json").read_text())["executable"]["entrypoints"][0]
SPEC = importlib.util.spec_from_file_location("content_scan", ROOT / "git-finalize-content-scan.py")
assert SPEC and SPEC.loader
SCANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCANNER)

HEADER = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
FOOTER = "-" * 5 + "END PRIVATE KEY" + "-" * 5
FIXTURE = f'KEY = """{HEADER}\nSYNTHETIC BODY ONE\n{FOOTER}"""\n'
OTHER_KEY = f'OTHER = """{HEADER}\nSYNTHETIC BODY TWO\n{FOOTER}"""\n'
TOKEN = "TOKEN = 'ghp_" + "B" * 24 + "'\n"


class ScannerBaselineTests(unittest.TestCase):
    def test_unchanged_matches_in_baseline_are_clear(self) -> None:
        base = FIXTURE.encode()
        self.assertEqual(SCANNER.scan(base), "PRIVATE")
        self.assertEqual(SCANNER.scan(base + b"\nVALUE = 1\n", baseline=base), "CLEAR")

    def test_new_key_under_an_existing_header_is_reported(self) -> None:
        base = FIXTURE.encode()
        self.assertEqual(SCANNER.scan(base + OTHER_KEY.encode(), baseline=base), "PRIVATE")
        replaced = FIXTURE.replace("BODY ONE", "BODY TWO").encode()
        self.assertEqual(SCANNER.scan(replaced, baseline=base), "PRIVATE")

    def test_new_token_beside_existing_fixture_is_reported(self) -> None:
        base = FIXTURE.encode()
        self.assertEqual(SCANNER.scan(base + TOKEN.encode(), baseline=base), "KNOWN")

    def test_unterminated_header_is_bound_to_its_line(self) -> None:
        base = (HEADER + " example\n").encode()
        self.assertEqual(SCANNER.scan(base + b"more text\n", baseline=base), "CLEAR")
        self.assertEqual(SCANNER.scan(base + base, baseline=base), "PRIVATE")

    def test_duplicated_existing_match_is_reported(self) -> None:
        base = TOKEN.encode()
        self.assertEqual(SCANNER.scan(base + base, baseline=base), "KNOWN")


class PublishBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="finalizer-baseline-scan-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
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
        self.git("config", "user.name", "Synthetic Baseline Tester")
        self.git("config", "user.email", "baseline@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("init", "-q", "--bare", "--initial-branch=main", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "tests").mkdir()
        self.path = self.repo / "tests" / "test_example.py"
        self.path.write_text(FIXTURE)
        self.git("add", "--all")
        self.git("commit", "-qm", "Synthetic published fixture")
        self.git("push", "-q", "origin", "main")
        self.git("switch", "-q", "-c", "feature")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], env=self.env, stderr=subprocess.PIPE
        ).decode().strip()

    def finalize(self, *args: str) -> dict:
        result = subprocess.run(
            [str(FINALIZER), "--summary", *args], env=self.env, capture_output=True,
            text=True, timeout=60, check=False,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def commit_and_publish(self, content: str) -> tuple[dict, dict]:
        self.path.write_text(content)
        committed = self.finalize(
            "--mode", "commit-only", "--repo", str(self.repo), "--message", "Edit fixture file",
            "--", "tests/test_example.py",
        )
        if committed["status"] != "success":
            return committed, {}
        published = self.finalize(
            "--publish-existing-branch", self.git("rev-parse", "HEAD"), "--remote", "origin",
            "--remote-branch", "feature", "--repo", str(self.repo),
        )
        return committed, published

    def test_editing_a_file_with_a_published_fixture_needs_no_review(self) -> None:
        committed, published = self.commit_and_publish(FIXTURE + "VALUE = 2\n")
        self.assertEqual(committed["status"], "success", committed)
        self.assertEqual(published["status"], "success", published)
        self.assertEqual(published["push"]["result"], "succeeded")

    def test_new_secret_in_that_file_is_still_refused(self) -> None:
        committed, _published = self.commit_and_publish(FIXTURE + TOKEN)
        self.assertEqual(committed["status"], "blocked", committed)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "main"))

    def test_new_secret_committed_outside_finalizer_is_refused_at_publication(self) -> None:
        self.path.write_text(FIXTURE + TOKEN)
        self.git("commit", "-qam", "Raw commit bypassing the commit-time scan")
        published = self.finalize(
            "--publish-existing-branch", self.git("rev-parse", "HEAD"), "--remote", "origin",
            "--remote-branch", "feature", "--repo", str(self.repo),
        )
        self.assertEqual(published["status"], "blocked", published)
        self.assertEqual(published["push"]["result"], "not_run")


if __name__ == "__main__":
    unittest.main()
