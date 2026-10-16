"""Synthetic patch artifacts retain required context spaces; other gates still refuse."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "git-finalize"
PATCH = b"--- a/example.txt\n+++ b/example.txt\n@@ -1,3 +1,3 @@\n start\n \n-old\n+new\n"
PATCH_EOF = b"--- a/example.txt\n+++ b/example.txt\n@@ -1,3 +1,3 @@\n start\n-old\n+new\n \n"


class PatchWhitespaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gf-patch-whitespace-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home), "PATH": os.defpath, "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.command("git", "init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        self.command("git", "init", "--quiet", "--initial-branch=main", str(self.repo))
        self.git("config", "user.name", "Synthetic Patch Author")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", "base.txt")
        self.git("commit", "--quiet", "-m", "base")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "--quiet", "--set-upstream", "origin", "main")
        self.base = self.git("rev-parse", "HEAD").strip()

    def command(self, *args, check=True):
        return subprocess.run(args, env=self.env, capture_output=True, text=True, check=check)

    def git(self, *args):
        return self.command("git", "-C", str(self.repo), *args).stdout

    def finalize(self, *args):
        return self.command(str(FINALIZER), "--repo", str(self.repo), *args, check=False)

    def assert_no_publication(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(self.command("git", "--git-dir", str(self.remote),
                                     "rev-parse", "main").stdout.strip(), self.base)

    def test_untracked_verify_is_non_mutating(self):
        path = self.repo / "change.patch"
        path.write_bytes(PATCH)
        before = (self.git("status", "--porcelain"), self.git("ls-files", "--stage"))
        result = self.finalize("--mode", "verify-only", "--", path.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, (self.git("status", "--porcelain"), self.git("ls-files", "--stage")))
        self.assertEqual(path.read_bytes(), PATCH)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)

    def test_tracked_verify_is_non_mutating(self):
        path = self.repo / "change.patch"
        path.write_text("old artifact\n")
        self.git("add", path.name)
        self.git("commit", "--quiet", "-m", "old artifact")
        path.write_bytes(PATCH)
        before = (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain"))
        result = self.finalize("--mode", "verify-only", "--", path.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, (self.git("rev-parse", "HEAD"), self.git("status", "--porcelain")))

    def test_commit_push_preserves_exact_patch_bytes(self):
        path = self.repo / "change.patch"
        path.write_bytes(PATCH)
        result = self.finalize("--message", "preserve patch", "--", path.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        blob = self.command("git", "-C", str(self.repo), "show", "HEAD:change.patch").stdout
        self.assertEqual(blob.encode(), PATCH)
        self.assertEqual(self.git("rev-list", "--left-right", "--count", "HEAD...@{upstream}").strip(), "0\t0")

    def test_existing_range_uses_the_same_context_classification(self):
        (self.repo / "change.patch").write_bytes(PATCH)
        self.git("add", "change.patch")
        self.git("commit", "--quiet", "-m", "preserved patch")
        oid = self.git("rev-parse", "HEAD").strip()
        result = self.finalize("--resume-publish", oid)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_required_empty_context_at_patch_eof_preserves_exact_bytes(self):
        path = self.repo / "change.patch"
        path.write_bytes(PATCH_EOF)
        before = (self.git("status", "--porcelain"), self.git("ls-files", "--stage"))
        result = self.finalize("--mode", "verify-only", "--", path.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, (self.git("status", "--porcelain"), self.git("ls-files", "--stage")))
        result = self.finalize("--message", "preserve final context", "--", path.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.command("git", "-C", str(self.repo), "show", "HEAD:change.patch").stdout.encode(), PATCH_EOF)
        path.write_bytes(PATCH_EOF.replace(b"+new", b"+next"))
        result = self.finalize("--mode", "verify-only", "--", path.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git("add", path.name)
        self.git("commit", "--quiet", "-m", "next final context")
        result = self.finalize("--resume-publish", self.git("rev-parse", "HEAD").strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_range_still_rejects_other_whitespace(self):
        (self.repo / "change.patch").write_bytes(PATCH)
        (self.repo / "bad.txt").write_text("ordinary trailing space \n")
        self.git("add", "change.patch", "bad.txt")
        self.git("commit", "--quiet", "-m", "synthetic bad range")
        oid = self.git("rev-parse", "HEAD").strip()
        result = self.finalize("--resume-publish", oid)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), oid)
        self.assertEqual(self.command("git", "--git-dir", str(self.remote),
                                     "rev-parse", "main").stdout.strip(), self.base)

    def test_other_whitespace_and_malformed_patches_still_refuse(self):
        cases = {
            "changed-line": PATCH.replace(b"+new\n", b"+new  \n"),
            "outside-hunk": PATCH + b" \n",
            "malformed": PATCH.replace(b"-1,3 +1,3", b"-1,4 +1,4"),
            "tab-context": PATCH.replace(b"\n \n", b"\n \t\n"),
            "oversized": PATCH + b"x" * 1_048_577 + b"\n",
            "outside-eof": PATCH_EOF + b"\n",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                path = self.repo / "change.patch"
                path.write_bytes(raw)
                result = self.finalize("--mode", "verify-only", "--", path.name)
                self.assert_no_publication(result)
                self.assertEqual(path.read_bytes(), raw)

    def test_non_patch_and_other_staged_whitespace_still_refuse(self):
        path = self.repo / "artifact.txt"
        path.write_bytes(PATCH)
        self.assert_no_publication(self.finalize("--mode", "verify-only", "--", path.name))
        path.unlink()
        (self.repo / "change.patch").write_bytes(PATCH)
        (self.repo / "bad.txt").write_text("trailing space \n")
        self.assert_no_publication(self.finalize("--message", "refuse mixed scope", "--",
                                                 "change.patch", "bad.txt"))

    def test_secret_content_is_not_exempted_by_patch_format(self):
        # A wholly artificial signature, assembled so the test source contains no key.
        marker = "-----" + "BEGIN" + " PRIVATE KEY" + "-----"
        raw = PATCH.replace(b"+new\n", b"+" + marker.encode() + b"\n")
        (self.repo / "change.patch").write_bytes(raw)
        self.assert_no_publication(self.finalize("--mode", "verify-only", "--", "change.patch"))

    def test_clean_filter_added_whitespace_is_rejected_after_staging(self):
        (self.repo / ".gitattributes").write_text("*.patch filter=patch-test\n")
        self.git("add", ".gitattributes")
        self.git("commit", "--quiet", "-m", "synthetic filter")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("push", "--quiet", "origin", "main")
        self.git("config", "filter.patch-test.clean", "sed 's/^+new$/+new  /'")
        (self.repo / "change.patch").write_bytes(PATCH)
        self.assert_no_publication(self.finalize("--message", "refuse filtered drift", "--", "change.patch"))


if __name__ == "__main__":
    unittest.main()
