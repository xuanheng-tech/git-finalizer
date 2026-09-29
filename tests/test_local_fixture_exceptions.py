from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import unittest

import test_fixture_exceptions as fixtures
import test_assignment_content as assignments


FAKE = "wrong_" + "secret" + " = '" + "SYNTHETIC_WRONG_RECONNECT_VALUE" + "'\n"
MODES = ("verify-only", "commit-only")


class LocalFixtureExceptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.FixtureExceptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.path = "backend/tests/test_terminal.py"
        self.write_content(FAKE)
        self.review()
        self.remote_calls = f.base / "remote-calls"
        bin_dir = f.base / "bin"
        bin_dir.mkdir()
        real_git = shutil.which("git", path=f.env["PATH"])
        self.assertIsNotNone(real_git)
        (bin_dir / "git").write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "if any(arg in {'fetch', 'push', 'ls-remote'} for arg in sys.argv[1:]):\n"
            "    Path(os.environ['FIXTURE_REMOTE_CALLS']).write_text('unexpected remote command\\n')\n"
            "    sys.exit(97)\n"
            "os.execv(os.environ['FIXTURE_REAL_GIT'], ['git', *sys.argv[1:]])\n"
        )
        (bin_dir / "git").chmod(0o700)
        f.env.update(
            PATH=str(bin_dir) + os.pathsep + f.env["PATH"],
            FIXTURE_REAL_GIT=real_git,
            FIXTURE_REMOTE_CALLS=str(self.remote_calls),
        )

    def write_content(self, content: str) -> None:
        target = self.fixture.repo / self.fixture.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def review(self, detector: str = "credential-assignment-v1") -> None:
        f = self.fixture
        raw = (f.repo / f.path).read_bytes()
        f.approval["exceptions"] = [{
            "path": f.path,
            "blob_oid": f.git("hash-object", "--no-filters", "--", f.path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "detector": detector,
            "reason": "Locally constructed wrong reconnect sentinel in an isolated test fixture",
        }]
        f.write_approval()

    def state(self) -> tuple[str, ...]:
        f = self.fixture
        return (
            f.git("rev-parse", "HEAD"),
            f.git("ls-files", "--stage"),
            f.git("status", "--porcelain=v1", "--untracked-files=all"),
            f.git("for-each-ref", "--format=%(refname) %(objectname)"),
            f.git("config", "--local", "--list"),
            f.git("--git-dir=" + str(f.remote), "for-each-ref", "--format=%(refname) %(objectname)"),
            hashlib.sha256((f.repo / ".git/index").read_bytes()).hexdigest(),
            hashlib.sha256((f.repo / f.path).read_bytes()).hexdigest(),
        )

    def run_finalizer(self, mode: str, *, approved: bool = True,
                      paths: list[str] | None = None, extra: tuple[str, ...] = ()) -> tuple[int, dict]:
        f = self.fixture
        command = [str(fixtures.FINALIZER), "--summary", "--mode", mode,
                   "--repo", str(f.repo)]
        if mode == "commit-only":
            command += ["--message", "Preserve unfinished synthetic test work"]
        if approved:
            command += ["--fixture-exceptions", str(f.file)]
        command += [*extra, "--", *(paths if paths is not None else [f.path])]
        result = subprocess.run(command, env=f.env, capture_output=True, text=True, timeout=30)
        self.assertFalse(self.remote_calls.exists(), "local review accessed a remote")
        self.assertNotIn("SYNTHETIC_WRONG_RECONNECT_VALUE", result.stdout + result.stderr)
        return result.returncode, json.loads(result.stdout)

    def assert_blocked(self, *, paths: list[str] | None = None,
                       approved: bool = True, extra: tuple[str, ...] = ()) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                before = self.state()
                code, data = self.run_finalizer(mode, paths=paths, approved=approved, extra=extra)
                self.assertNotEqual(code, 0, data)
                self.assertEqual(data["status"], "blocked")
                self.assertFalse(data["commit"]["created"])
                self.assertFalse(data["push"]["executed"])
                self.assertEqual(before, self.state())

    def test_exact_verify_is_read_only_then_commit_preserves_the_raw_blob(self) -> None:
        f = self.fixture
        before = self.state()
        code, verified = self.run_finalizer("verify-only")
        self.assertEqual(code, 0, verified)
        self.assertEqual(before, self.state())
        self.assertEqual(verified["mode"], "verify-only")
        self.assertFalse(verified["commit"]["created"])
        self.assertFalse(verified["push"]["executed"])
        (receipt,) = verified["fixture_exceptions"]
        self.assertEqual(receipt["blob_oid"], f.approval["exceptions"][0]["blob_oid"])
        self.assertEqual(receipt["sha256"], f.approval["exceptions"][0]["sha256"])
        code, committed = self.run_finalizer("commit-only")
        self.assertEqual(code, 0, committed)
        self.assertTrue(committed["commit"]["created"])
        self.assertFalse(committed["push"]["executed"])
        self.assertEqual(committed["fixture_exceptions"], verified["fixture_exceptions"])
        self.assertEqual(f.git("rev-parse", "HEAD:" + f.path), receipt["blob_oid"])
        self.assertEqual(f.git("status", "--porcelain=v1"), "")
        self.assertEqual(self.state()[4:6], before[4:6])
        self.assertEqual(self.state()[7], before[7])

    def test_credential_review_does_not_skip_escaped_schema_signatures(self) -> None:
        variants = (
            ("github" + "_pat_" + assignments.OPAQUE_VALUE, b"github_pat_", b"github\\u005fpat_"),
            ("-----BEGIN " + "PRIVATE KEY-----", b"PRIVATE", b"PRIV\\u0041TE"),
            (" ssh-" + "ed25519 " + "A" * 48, b"ssh-", b"ss\\u0068-"),
        )
        for index, (value, literal, escaped) in enumerate(variants):
            for staged in (False, True):
                for mode in MODES:
                    with self.subTest(kind=index, staged=staged, mode=mode):
                        document = assignments.schema()
                        document["wrong_secret"] = "SYNTHETIC_WRONG_RECONNECT_VALUE"
                        document["description"] = value
                        raw = json.dumps(document).encode().replace(literal, escaped)
                        self.write_content(raw.decode())
                        self.review()
                        if staged:
                            self.fixture.git("add", "--", self.fixture.path)
                        before = self.state()
                        code, summary = self.run_finalizer(mode)
                        self.assertNotEqual(code, 0, summary)
                        self.assertFalse(summary["commit"]["created"])
                        self.assertFalse(summary["push"]["executed"])
                        self.assertEqual(before, self.state())
                        self.fixture.git("reset", "-q", "HEAD", "--", self.fixture.path)

    def test_default_scan_and_legacy_blanket_do_not_accept_backend_fixture(self) -> None:
        self.assert_blocked(approved=False)
        self.assert_blocked(approved=False, extra=("--allow-test-fixture", self.fixture.path))

    def test_repository_blob_hash_and_detector_bindings_are_exact(self) -> None:
        f = self.fixture
        original = copy.deepcopy(f.approval)
        for section, key, value in (
            ("repository", "root_commit", "f" * 40),
            ("repository", "remote_url_sha256", "f" * 64),
            ("exception", "blob_oid", "f" * 40),
            ("exception", "sha256", "f" * 64),
            ("exception", "detector", "known-token-v1"),
            ("exception", "detector", "private-key-header-v1"),
            ("exception", "detector", "unknown-detector"),
            ("exception", "path", "backend/tests/../tests/test_terminal.py"),
            ("exception", "path", "backend/tests/test_*.py"),
        ):
            with self.subTest(section=section, key=key, value=value):
                f.approval = copy.deepcopy(original)
                target = f.approval["repository"] if section == "repository" else f.approval["exceptions"][0]
                target[key] = value
                f.write_approval()
                self.assert_blocked()

    def test_production_nested_wrong_suffix_and_legacy_paths_are_rejected(self) -> None:
        f = self.fixture
        for path in ("backend/src/test_terminal.py", "backend/tests/sub/test_terminal.py",
                     "backend/tests/test_terminal.txt", "backend/tests/terminal.py",
                     "other/tests/test_terminal.py", "tests/example.py", "CHANGELOG.md"):
            with self.subTest(path=path):
                f.path = path
                self.write_content(FAKE)
                self.review()
                self.assert_blocked()

    def test_known_token_private_and_ssh_key_signatures_are_never_exempt(self) -> None:
        for signature in ("ghp_" + "SYNTHETICONLY" * 3 + "\n", fixtures.SYNTHETIC,
                          "ssh-" + "rsa " + "A" * 80 + " synthetic@example.invalid\n"):
            with self.subTest(signature_type=signature.split()[0]):
                self.write_content(FAKE + signature)
                self.review()
                self.assert_blocked()

    def test_changed_content_same_blob_other_path_and_missing_signature_are_rejected(self) -> None:
        f = self.fixture
        self.write_content(FAKE + "# Changed after review\n")
        self.assert_blocked()
        self.write_content("# No credential assignment\n")
        self.review()
        self.assert_blocked()
        self.write_content(FAKE)
        self.review()
        other = "backend/tests/test_other.py"
        (f.repo / other).write_text(FAKE)
        self.assert_blocked(paths=[f.path, other])

    def test_out_of_scope_unchanged_and_symlink_reviews_are_rejected(self) -> None:
        f = self.fixture
        self.assert_blocked(paths=["README.md"])
        f.commit()
        (f.repo / "README.md").write_text("Unrelated candidate\n")
        self.assert_blocked(paths=[f.path, "README.md"])
        target = f.repo / f.path
        target.unlink()
        target.symlink_to(f.repo / "README.md")
        self.assert_blocked()

    def test_existing_changed_index_must_equal_the_reviewed_raw_blob(self) -> None:
        f = self.fixture
        self.write_content(FAKE + "# Staged earlier\n")
        f.git("add", "--", f.path)
        self.write_content(FAKE)
        self.review()
        self.assert_blocked()
        f.git("add", "--", f.path)
        before = self.state()
        code, data = self.run_finalizer("verify-only")
        self.assertEqual(code, 0, data)
        self.assertEqual(before, self.state())

    def test_clean_filter_conversion_is_rejected_before_staging(self) -> None:
        f = self.fixture
        f.git("config", "filter.synthetic.clean", "sed s/SYNTHETIC/ALTERED/g")
        (f.repo / ".gitattributes").write_text(f.path + " filter=synthetic\n")
        self.assert_blocked()

    def test_ambiguous_or_missing_origin_and_mixed_blanket_reviews_are_rejected(self) -> None:
        f = self.fixture
        self.assert_blocked(extra=("--allow-test-fixture", f.path))
        f.git("config", "--add", "remote.origin.pushurl", str(f.remote))
        f.git("config", "--add", "remote.origin.pushurl", str(f.remote) + "-other")
        self.assert_blocked()
        f.git("remote", "remove", "origin")
        self.assert_blocked()


if __name__ == "__main__":
    unittest.main()
