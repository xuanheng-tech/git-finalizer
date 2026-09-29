from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "git-finalize"


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments), check=False, capture_output=True,
        text=True, env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class IntegrationPublishFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="git-finalizer-publication-")
        self.root = Path(self.temporary.name)
        self.repo, self.remote, self.candidate = (
            self.root / "repo", self.root / "remote.git", self.root / "candidate",
        )
        git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(self.remote, "config", "core.logAllRefUpdates", "true")
        git(self.root, "init", "--initial-branch=main", str(self.repo))
        git(self.repo, "config", "user.name", "Publication Test")
        git(self.repo, "config", "user.email", "publication@example.invalid")
        (self.repo / "base.txt").write_text("base\n")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "--set-upstream", "origin", "main")
        self.expected_main = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "worktree", "add", "-b", "integration/candidate", str(self.candidate), "main")
        (self.candidate / "candidate.txt").write_text("candidate\n")
        git(self.candidate, "add", "candidate.txt")
        git(self.candidate, "commit", "-m", "candidate")
        self.candidate_oid = git(self.candidate, "rev-parse", "HEAD")
        self.lease_id = str(uuid.uuid4())
        self.run_id = "integration-publish-test"
        self.identity = {
            "repository_id": str(uuid.uuid4()), "allocation_id": str(uuid.uuid4()),
            "lease_id": self.lease_id, "run_id": self.run_id, "remote": "origin",
            "target_ref": "refs/heads/main", "candidate_oid": self.candidate_oid,
            "expected_main_oid": self.expected_main, "validation_evidence_id": "validation-evidence",
            "record_version": 3,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        self.config_path = self.root / "public-contract.json"
        self.config: dict[str, object] = {"repository": str(self.candidate), "identity": self.identity}
        self.write_config()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        controller = self.bin / "worktree-controller"
        controller.write_text(
            f"#!{sys.executable}\nimport os, sys\nos.execv({sys.executable!r}, "
            f"[{sys.executable!r}, {str(ROOT / 'tests/publication_controller_fixture.py')!r}, "
            f"{str(self.config_path)!r}, *sys.argv[1:]])\n"
        )
        controller.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def close(self) -> None:
        self.temporary.cleanup()

    def write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config) + "\n")

    def command(self) -> list[str]:
        return [
            str(FINALIZER), "--publish-integration-candidate", self.candidate_oid,
            "--repo", str(self.candidate), "--lease-id", self.lease_id,
            "--run-id", self.run_id, "--summary",
        ]

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(), check=False, capture_output=True, text=True,
            env={**self.environment, **extra}, timeout=30,
        )

    def remote_reflog_count(self) -> int:
        return len(git(self.remote, "reflog", "show", "--format=%H", "refs/heads/main").splitlines())

    def dirty_canonical(self) -> dict[str, object]:
        (self.repo / "base.txt").write_text("unrelated staged\n")
        git(self.repo, "add", "base.txt")
        (self.repo / "untracked.txt").write_text("unrelated untracked\n")
        return self.canonical_state()

    def canonical_state(self) -> dict[str, object]:
        index = Path(git(self.repo, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.repo / index
        return {
            "base": (self.repo / "base.txt").read_bytes(),
            "untracked": (self.repo / "untracked.txt").read_bytes(),
            "index": index.read_bytes(), "head_ref": git(self.repo, "symbolic-ref", "HEAD"),
            "main_oid": git(self.repo, "rev-parse", "refs/heads/main"),
            "status": subprocess.run(
                ("git", "-C", str(self.repo), "status", "--porcelain=v1", "-z"),
                check=True, capture_output=True,
            ).stdout,
        }


class IntegrationPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = IntegrationPublishFixture()
        self.addCleanup(self.fixture.close)

    def assert_blocked(self, result: subprocess.CompletedProcess[str], reason: str) -> dict[str, object]:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "blocked")
        self.assertIn(reason, summary["reason"])
        return summary

    def test_publication_needs_no_private_controller_metadata_and_preserves_canonical(self) -> None:
        before = self.fixture.dirty_canonical()
        common = Path(git(self.fixture.candidate, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertFalse((common / "worktree-controller").exists())
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["remote_verify"]["oid"], self.fixture.candidate_oid)
        self.assertEqual(summary["controller_execution"]["contract"], "integration-publication-session/v1")
        self.assertTrue(summary["controller_execution"]["lifecycle_completion_required"])
        self.assertFalse((common / "worktree-controller").exists())
        self.assertEqual(self.fixture.canonical_state(), before)

    def test_interrupted_push_recovers_without_duplicate_mutation(self) -> None:
        before = self.fixture.remote_reflog_count()
        interrupted = self.fixture.run(GIT_FINALIZER_TEST_CRASH_AFTER_PUSH="1")
        self.assertEqual(interrupted.returncode, 97, interrupted.stdout + interrupted.stderr)
        self.assertEqual(self.fixture.remote_reflog_count(), before + 1)
        recovered = self.fixture.run()
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        summary = json.loads(recovered.stdout)
        self.assertEqual(summary["push"]["result"], "already_published_recovered")
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.fixture.remote_reflog_count(), before + 1)

    def test_duplicate_consumers_share_the_public_session_gate(self) -> None:
        before = self.fixture.remote_reflog_count()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.fixture.run(), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summaries = [json.loads(result.stdout) for result in results]
        self.assertEqual(sum(result["push"]["executed"] for result in summaries), 1)
        self.assertEqual(self.fixture.remote_reflog_count(), before + 1)

    def test_failed_push_leaves_same_candidate_for_recovery(self) -> None:
        hook = self.fixture.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        before = self.fixture.remote_reflog_count()
        self.assert_blocked(self.fixture.run(), "integration push failed")
        self.assertEqual(self.fixture.remote_reflog_count(), before)
        hook.unlink()
        recovered = self.fixture.run()
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(git(self.fixture.candidate, "rev-parse", "HEAD"), self.fixture.candidate_oid)

    def test_completion_refusal_after_push_blocks_then_recovers_without_repeating_push(self) -> None:
        self.fixture.config["refusal"] = "completed"
        self.fixture.write_config()
        before = self.fixture.remote_reflog_count()
        self.assert_blocked(self.fixture.run(), "FIXTURE_REFUSAL")
        self.assertEqual(self.fixture.remote_reflog_count(), before + 1)
        del self.fixture.config["refusal"]
        self.fixture.write_config()
        recovered = self.fixture.run()
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertFalse(json.loads(recovered.stdout)["push"]["executed"])
        self.assertEqual(self.fixture.remote_reflog_count(), before + 1)

    def test_old_or_malformed_capability_cannot_fall_back_to_private_files(self) -> None:
        before = self.fixture.remote_reflog_count()
        for capability in (0, 2, True, None):
            with self.subTest(capability=capability):
                self.fixture.config["capability"] = capability
                self.fixture.write_config()
                self.assert_blocked(self.fixture.run(), "required public integration publication protocol")
                self.assertEqual(self.fixture.remote_reflog_count(), before)

    def test_missing_controller_refuses_before_push(self) -> None:
        before = self.fixture.remote_reflog_count()
        self.assert_blocked(self.fixture.run(PATH="/usr/bin:/bin"), "Worktree Controller is required")
        self.assertEqual(self.fixture.remote_reflog_count(), before)

    def test_unsafe_remote_and_wrong_candidate_identity_are_rejected(self) -> None:
        before = self.fixture.remote_reflog_count()
        for field, value in (("remote", "-unsafe"), ("candidate_oid", self.fixture.expected_main), ("record_version", True)):
            with self.subTest(field=field):
                changed = {**self.fixture.identity, field: value}
                self.fixture.config["overrides"] = {"verified": {"identity": changed}}
                self.fixture.write_config()
                self.assert_blocked(self.fixture.run(), "Controller")
                self.assertEqual(self.fixture.remote_reflog_count(), before)

    def test_schema_phase_and_authorization_identity_drift_are_rejected(self) -> None:
        before = self.fixture.remote_reflog_count()
        for phase, override in (
            ("verified", {"schema_version": True}),
            ("verified", {"contract": "unsupported/v2"}),
            ("verified", {"action": {}}),
            ("authorized", {"action": []}),
            ("authorized", {"phase": "completed"}),
            ("authorized", {"identity": {**self.fixture.identity, "record_version": False}}),
            ("authorized", {"identity": {**self.fixture.identity, "run_id": "wrong-run"}}),
        ):
            with self.subTest(phase=phase, override=override):
                self.fixture.config["overrides"] = {phase: override}
                self.fixture.write_config()
                self.assert_blocked(self.fixture.run(), "Controller")
                self.assertEqual(self.fixture.remote_reflog_count(), before)

    def test_duplicate_json_response_is_rejected_before_mutation(self) -> None:
        self.fixture.config["raw"] = {"verified": '{"schema_version":1,"schema_version":1}'}
        self.fixture.write_config()
        before = self.fixture.remote_reflog_count()
        self.assert_blocked(self.fixture.run(), "duplicate JSON key")
        self.assertEqual(self.fixture.remote_reflog_count(), before)

    def test_exact_branch_push_does_not_follow_configured_tags(self) -> None:
        git(self.fixture.candidate, "config", "push.followTags", "true")
        git(self.fixture.candidate, "tag", "-a", "candidate-tag", "-m", "candidate tag")
        result = self.fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(self.fixture.remote, "for-each-ref", "--format=%(refname)", "refs/tags/"), "")

    def test_session_timeout_is_bounded_and_preserves_remote(self) -> None:
        self.fixture.config["delays"] = {"verified": 0.5}
        self.fixture.write_config()
        spec = importlib.util.spec_from_file_location("publication_consumer", ROOT / "git-finalize-integration-publish.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        before = self.fixture.remote_reflog_count()
        with patch.dict(os.environ, self.fixture.environment), patch.object(module, "RESPONSE_TIMEOUT_SECONDS", 0.05):
            arguments = module.build_parser().parse_args(self.fixture.command()[1:])
            with self.assertRaisesRegex(module.PublishError, "timed out"):
                module.publish(arguments)
        self.assertEqual(self.fixture.remote_reflog_count(), before)


if __name__ == "__main__":
    unittest.main()
