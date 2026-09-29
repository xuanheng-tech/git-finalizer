from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import unittest

import test_fixture_exceptions as fixtures


HEADER = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
BASELINE = "# Changelog\n\n## 0.1.0\n\nExample: `" + HEADER + "` is a header literal.\nHistorical notes.\n"
PREPARED = BASELINE.replace("## 0.1.0", "## 0.2.0\n\nPrepared release notes.\n\n## 0.1.0")


class PublicationExampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.FixtureExceptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.path = "CHANGELOG.md"
        (f.repo / f.path).write_text(BASELINE)
        f.commit()
        f.git("push", "--quiet", "--set-upstream", "origin", "main")
        self.baseline_oid = f.git("rev-parse", "HEAD")
        (f.repo / f.path).write_text(PREPARED)
        self.review()

    def review(self) -> None:
        f = self.fixture
        raw = (f.repo / f.path).read_bytes()
        f.approval["exceptions"] = [{
            "path": f.path,
            "blob_oid": f.git("hash-object", "--no-filters", "--", f.path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "detector": "private-key-header-v1",
            "reason": "Preserve the already-published inline header and historical suffix",
        }]
        f.write_approval()

    def run_finalizer(self, *, resume: bool = False, approved: bool = True):
        f = self.fixture
        command = [str(fixtures.FINALIZER), "--summary", "--repo", str(f.repo)]
        if resume:
            command += ["--resume-publish", f.git("rev-parse", "HEAD")]
        else:
            command += ["--message", "Prepare the next release"]
        if approved:
            command += ["--fixture-exceptions", str(f.file)]
        if not resume:
            command += ["--", f.path]
        result = subprocess.run(command, env=f.env, capture_output=True, text=True, timeout=30)
        self.assertNotIn(HEADER, result.stdout + result.stderr)
        return result.returncode, json.loads(result.stdout)

    def remote_oid(self) -> str:
        f = self.fixture
        return f.git("--git-dir=" + str(f.remote), "rev-parse", "refs/heads/main")

    def assert_blocked(self, **kwargs):
        before = self.remote_oid()
        head = self.fixture.git("rev-parse", "HEAD")
        code, data = self.run_finalizer(**kwargs)
        self.assertNotEqual(code, 0, data)
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.remote_oid(), before)
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), head)
        return data

    def test_normal_review_checks_working_and_staged_blob_then_verifies_remote(self) -> None:
        f = self.fixture
        tags = f.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags")
        code, data = self.run_finalizer()
        self.assertEqual(code, 0, data)
        self.assertEqual(self.remote_oid(), f.git("rev-parse", "HEAD"))
        self.assertEqual(f.git("status", "--porcelain=v1"), "")
        self.assertEqual(data["mode_result"]["post_verify"], "passed")
        (receipt,) = data["fixture_exceptions"]
        self.assertEqual(receipt["blob_oid"], f.approval["exceptions"][0]["blob_oid"])
        self.assertEqual(receipt["sha256"], f.approval["exceptions"][0]["sha256"])
        self.assertEqual(tags, f.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags"))

    def test_normal_also_binds_an_exact_synthetic_test_fixture(self) -> None:
        f = self.fixture
        (f.repo / f.path).write_text(BASELINE)
        f.path = "tests/example.py"
        (f.repo / f.path).write_text(fixtures.SYNTHETIC + "Updated synthetic fixture\n")
        self.review()
        self.assert_blocked(approved=False)
        code, data = self.run_finalizer()
        self.assertEqual(code, 0, data)
        self.assertEqual(self.remote_oid(), f.git("rev-parse", "HEAD"))

    def test_normal_and_resume_keep_default_refusal_without_exact_review(self) -> None:
        self.assert_blocked(approved=False)
        self.assertEqual(self.fixture.git("diff", "--cached"), "")
        self.fixture.commit()
        self.assert_blocked(resume=True, approved=False)

    def test_resume_uses_live_upstream_baseline_without_recreating_commit(self) -> None:
        f = self.fixture
        f.commit()
        head = f.git("rev-parse", "HEAD")
        config = f.git("config", "--local", "--list")
        code, data = self.run_finalizer(resume=True)
        self.assertEqual(code, 0, data)
        self.assertEqual(head, f.git("rev-parse", "HEAD"))
        self.assertEqual(head, self.remote_oid())
        self.assertEqual(config, f.git("config", "--local", "--list"))
        self.assertEqual(len(data["fixture_exceptions"]), 1)

    def test_repository_path_blob_hash_and_rule_bindings_remain_exact(self) -> None:
        f = self.fixture
        original = copy.deepcopy(f.approval)
        for section, key, value in (
            ("repository", "root_commit", "f" * 40),
            ("repository", "remote_url_sha256", "f" * 64),
            ("exception", "path", "README.md"),
            ("exception", "path", "tests/../CHANGELOG.md"),
            ("exception", "blob_oid", "f" * 40),
            ("exception", "sha256", "f" * 64),
            ("exception", "detector", "known-token-v1"),
            ("exception", "detector", "unknown-detector"),
        ):
            with self.subTest(key=key, value=value):
                f.approval = copy.deepcopy(original)
                target = f.approval[section] if section == "repository" else f.approval["exceptions"][0]
                target[key] = value
                f.write_approval()
                self.assert_blocked()

    def test_added_unquoted_changed_or_closed_headers_are_refused(self) -> None:
        f = self.fixture
        variants = (
            PREPARED.replace("`" + HEADER + "`", HEADER),
            "New example: `" + HEADER + "`\n" + PREPARED,
            PREPARED.replace("Historical notes.", "Historical notes changed."),
            "-" * 5 + "END PRIVATE KEY" + "-" * 5 + "\n" + PREPARED,
        )
        for content in variants:
            with self.subTest(content_length=len(content)):
                (f.repo / f.path).write_text(content)
                self.review()
                self.assert_blocked()

    def test_other_content_detectors_still_block_the_reviewed_document(self) -> None:
        f = self.fixture
        variants = (
            "gh" + "p_" + "SYNTHETICONLY" * 3 + "\n" + PREPARED,
            "pass" + 'word = "' + "SYNTHETIC_VALUE_ONLY" + '"\n' + PREPARED,
            "ssh" + "-ed25519 " + "A" * 44 + "\n" + PREPARED,
        )
        for content in variants:
            with self.subTest(content_length=len(content)):
                (f.repo / f.path).write_text(content)
                self.review()
                self.assert_blocked()

    def test_working_blob_drift_invalidates_the_prepared_review(self) -> None:
        f = self.fixture
        (f.repo / f.path).write_text(PREPARED.replace("Prepared", "Changed"))
        self.assert_blocked()

    def test_clean_filter_drift_stops_before_commit_or_push(self) -> None:
        f = self.fixture
        (f.repo / f.path).write_text(BASELINE)
        (f.repo / ".gitattributes").write_text("CHANGELOG.md filter=review-example\n")
        f.git("config", "filter.review-example.clean", "sed 's/Prepared/Filtered/'")
        f.commit()
        f.git("push", "--quiet", "origin", "main")
        (f.repo / f.path).write_text(PREPARED)
        self.review()
        self.assert_blocked()
        self.assertNotEqual(f.git("rev-parse", ":" + f.path), f.approval["exceptions"][0]["blob_oid"])

    def test_normal_review_requires_no_unpublished_commits(self) -> None:
        f = self.fixture
        (f.repo / "other.md").write_text("An unpublished ordinary change\n")
        f.git("add", "--", "other.md")
        f.git("commit", "-qm", "Unpublished ordinary change")
        self.assert_blocked()
        self.assertEqual(f.git("diff", "--cached"), "")

    def test_unused_unchanged_document_review_is_refused(self) -> None:
        f = self.fixture
        (f.repo / f.path).write_text(BASELINE)
        self.review()
        self.assert_blocked()


class ResumedFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.FixtureExceptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.git("push", "--quiet", "origin", f.root_oid + ":refs/heads/main")
        f.git("branch", "--set-upstream-to=origin/main", "main")

    def run_finalizer(self, *, approved: bool = True):
        f = self.fixture
        head = f.git("rev-parse", "HEAD")
        index = f.git("ls-files", "--stage")
        command = [str(fixtures.FINALIZER), "--summary", "--repo", str(f.repo), "--resume-publish", head]
        if approved:
            command += ["--fixture-exceptions", str(f.file)]
        result = subprocess.run(command, env=f.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(head, f.git("rev-parse", "HEAD"))
        self.assertEqual(index, f.git("ls-files", "--stage"))
        self.assertEqual(f.git("status", "--porcelain=v1"), "")
        return result.returncode, json.loads(result.stdout)

    def test_resume_accepts_exact_synthetic_fixture_only_when_reviewed(self) -> None:
        code, data = self.run_finalizer(approved=False)
        self.assertNotEqual(code, 0)
        self.assertFalse(data["push"]["executed"])
        code, data = self.run_finalizer()
        self.assertEqual(code, 0, data)
        self.assertEqual(len(data["fixture_exceptions"]), 1)
        self.assertEqual(data["mode_result"]["post_verify"], "passed")

    def test_resume_does_not_exempt_a_new_blob_or_another_path(self) -> None:
        f = self.fixture
        (f.repo / f.path).write_text(fixtures.SYNTHETIC + "Changed\n")
        f.commit()
        code, data = self.run_finalizer()
        self.assertNotEqual(code, 0)
        self.assertFalse(data["push"]["executed"])


if __name__ == "__main__":
    unittest.main()
