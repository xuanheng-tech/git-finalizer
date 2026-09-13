from __future__ import annotations

import copy
import hashlib
import json
import unittest

import test_fixture_exceptions as fixtures


MODE = "--publish-existing-branch"
SYNTHETIC = fixtures.SYNTHETIC


class BranchFixtureExceptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.FixtureExceptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.git("push", "--quiet", "origin", fixture.root_oid + ":refs/heads/main")
        fixture.git("branch", "-m", "feature/fixture")

    def refs(self) -> str:
        fixture = self.fixture
        return fixture.git("--git-dir=" + str(fixture.remote), "for-each-ref",
                           "--format=%(objectname) %(refname)")

    def assert_blocked(self, *, approved: bool = True) -> None:
        before = self.refs()
        code, receipt = self.fixture.run_finalizer(MODE, approved=approved)
        self.assertNotEqual(code, 0, receipt)
        self.assertEqual(receipt["status"], "blocked")
        self.assertFalse(receipt["push"]["executed"])
        self.assertEqual(self.refs(), before)

    def test_exact_exception_first_publishes_without_replaying_a_commit(self) -> None:
        self.fixture.assert_exact_pass(MODE)

    def test_missing_exception_and_changed_blob_remain_blocked(self) -> None:
        self.assert_blocked(approved=False)
        fixture = self.fixture
        (fixture.repo / fixture.path).write_text(SYNTHETIC + "X")
        fixture.commit()
        self.assert_blocked()

    def test_same_blob_at_another_path_is_not_exempt(self) -> None:
        fixture = self.fixture
        (fixture.repo / "tests/other.py").write_text(SYNTHETIC)
        fixture.commit()
        self.assert_blocked()

    def test_removing_the_signature_does_not_reuse_old_content_approval(self) -> None:
        fixture = self.fixture
        (fixture.repo / fixture.path).write_text("+" + SYNTHETIC[1:])
        fixture.commit()
        self.assert_blocked()

    def test_another_detector_in_the_same_blob_is_not_exempt(self) -> None:
        fixture = self.fixture
        content = SYNTHETIC + "ghp_" + "SYNTHETICONLY" * 3 + "\n"
        (fixture.repo / fixture.path).write_text(content)
        fixture.commit()
        fixture.approval["exceptions"].append(dict(
            fixture.approval["exceptions"][0],
            blob_oid=fixture.git("rev-parse", "HEAD:" + fixture.path),
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        ))
        fixture.write_approval()
        self.assert_blocked()

    def test_repository_content_rule_and_exact_path_bindings_remain_mandatory(self) -> None:
        fixture = self.fixture
        original = copy.deepcopy(fixture.approval)
        for section, key, value in (
            ("repository", "root_commit", "f" * 40),
            ("repository", "remote_url_sha256", "f" * 64),
            ("exception", "blob_oid", "f" * 40),
            ("exception", "sha256", "f" * 64),
            ("exception", "detector", "known-token-v1"),
            ("exception", "detector", "unknown-detector"),
            ("exception", "path", "tests/*.py"),
            ("exception", "path", "tests/../example.py"),
        ):
            with self.subTest(section=section, key=key, value=value):
                fixture.approval = copy.deepcopy(original)
                target = (fixture.approval["repository"] if section == "repository"
                          else fixture.approval["exceptions"][0])
                target[key] = value
                fixture.write_approval()
                self.assert_blocked()

    def test_malformed_duplicate_and_unknown_schema_approvals_are_refused(self) -> None:
        fixture = self.fixture
        for raw in (
            "{",
            '{"schema_version":1,"schema_version":1}',
            json.dumps(dict(fixture.approval, schema_version=2)),
            json.dumps(dict(fixture.approval, exceptions=fixture.approval["exceptions"] * 2)),
        ):
            with self.subTest(raw=raw):
                fixture.file.write_text(raw)
                self.assert_blocked()

    def test_unused_exception_is_refused_when_history_is_already_published(self) -> None:
        fixture = self.fixture
        fixture.git("push", "--quiet", "origin", "HEAD:refs/heads/reviewed-source")
        self.assert_blocked()

    def test_existing_target_is_not_overwritten_even_with_exact_approval(self) -> None:
        fixture = self.fixture
        fixture.git("push", "--quiet", "origin", fixture.root_oid + ":refs/heads/feature/fixture")
        self.assert_blocked()


if __name__ == "__main__":
    unittest.main()
