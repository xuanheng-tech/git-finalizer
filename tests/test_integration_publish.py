from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "codex-git-finalize"


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class IntegrationPublishFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="git-finalizer-integration-"
        )
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.candidate = self.root / "candidate"
        git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(self.remote, "config", "core.logAllRefUpdates", "true")
        git(self.root, "init", "--initial-branch=main", str(self.repo))
        git(self.repo, "config", "user.name", "Integration Test")
        git(self.repo, "config", "user.email", "integration@example.invalid")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "--set-upstream", "origin", "main")
        self.expected_main = git(self.repo, "rev-parse", "HEAD")
        git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "integration/candidate",
            str(self.candidate),
            "main",
        )
        (self.candidate / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        git(self.candidate, "add", "candidate.txt")
        git(self.candidate, "commit", "-m", "candidate")
        self.candidate_oid = git(self.candidate, "rev-parse", "HEAD")
        self.common = Path(
            git(
                self.candidate,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        )
        git_dir = Path(
            git(
                self.candidate,
                "rev-parse",
                "--path-format=absolute",
                "--absolute-git-dir",
            )
        )
        relative_git_dir = git_dir.relative_to(self.common)
        self.worktree_key = (
            "wt_" + hashlib.sha256(os.fspath(relative_git_dir).encode()).hexdigest()
        )
        self.metadata = self.common / "codex-worktree" / "v1"
        self.allocation_id = str(uuid.uuid4())
        self.repository_id = str(uuid.uuid4())
        self.lease_id = str(uuid.uuid4())
        self.intent_id = str(uuid.uuid4())
        self.run_id = "integration-publish-test"
        self.validation_evidence = "validation-evidence"
        self._write_metadata(datetime.now(timezone.utc) + timedelta(minutes=5))

    def close(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def _write_metadata(self, expires: datetime) -> None:
        self.metadata.mkdir(parents=True, exist_ok=True)
        (self.metadata / "repo.lock").touch()
        self._write(
            self.metadata / "repo.json",
            {"repository_id": self.repository_id},
        )
        self._write(
            self.metadata / "bindings" / f"{self.worktree_key}.json",
            {"allocation_id": self.allocation_id},
        )
        self._write(
            self.metadata / "records" / f"{self.allocation_id}.json",
            {
                "allocation_id": self.allocation_id,
                "role": "integration",
                "lifecycle": "INTEGRATING",
                "owner": "integration-owner",
                "task_commit": self.candidate_oid,
                "handoff_commit": self.candidate_oid,
                "finalizer_record_id": self.validation_evidence,
                "finalizer_commit": self.candidate_oid,
            },
        )
        self._write(
            self.metadata / "integration-intents" / f"{self.intent_id}.json",
            {
                "allocation_id": self.allocation_id,
                "state": "VALIDATED",
                "candidate_oid": self.candidate_oid,
                "validated_oid": self.candidate_oid,
                "validation_evidence_id": self.validation_evidence,
            },
        )
        self._write(
            self.metadata / "publication-lease.json",
            {
                "schema_version": 2,
                "lease_id": self.lease_id,
                "repository_id": self.repository_id,
                "allocation_id": self.allocation_id,
                "holder": "integration-owner",
                "run_id": self.run_id,
                "remote": "origin",
                "branch": "main",
                "target_ref": "refs/heads/main",
                "candidate_oid": self.candidate_oid,
                "expected_main_oid": self.expected_main,
                "validation_evidence_id": self.validation_evidence,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": expires.isoformat(),
            },
        )

    def command(self) -> tuple[str, ...]:
        return (
            str(FINALIZER),
            "--publish-integration-candidate",
            self.candidate_oid,
            "--repo",
            str(self.candidate),
            "--lease-id",
            self.lease_id,
            "--run-id",
            self.run_id,
            "--summary",
        )

    def remote_reflog_count(self) -> int:
        output = git(self.remote, "reflog", "show", "--format=%H", "refs/heads/main")
        return len(output.splitlines()) if output else 0

    def dirty_canonical(self) -> dict[str, object]:
        base = self.repo / "base.txt"
        base.write_text("staged\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        base.write_text("staged-and-modified\n", encoding="utf-8")
        untracked = self.repo / "untracked.txt"
        untracked.write_text("untracked\n", encoding="utf-8")
        index = Path(git(self.repo, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.repo / index
        return {
            "base": base.read_bytes(),
            "untracked": untracked.read_bytes(),
            "index": index.read_bytes(),
            "head_ref": git(self.repo, "symbolic-ref", "HEAD"),
            "main_oid": git(self.repo, "rev-parse", "refs/heads/main"),
            "status": subprocess.run(
                ("git", "-C", str(self.repo), "status", "--porcelain=v1", "-z"),
                check=True,
                capture_output=True,
            ).stdout,
        }

    def canonical_state(self) -> dict[str, object]:
        base = self.repo / "base.txt"
        untracked = self.repo / "untracked.txt"
        index = Path(git(self.repo, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.repo / index
        return {
            "base": base.read_bytes(),
            "untracked": untracked.read_bytes(),
            "index": index.read_bytes(),
            "head_ref": git(self.repo, "symbolic-ref", "HEAD"),
            "main_oid": git(self.repo, "rev-parse", "refs/heads/main"),
            "status": subprocess.run(
                ("git", "-C", str(self.repo), "status", "--porcelain=v1", "-z"),
                check=True,
                capture_output=True,
            ).stdout,
        }


class IntegrationPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = IntegrationPublishFixture()
        self.addCleanup(self.fixture.close)

    def test_interrupted_publish_recovers_without_duplicate_remote_mutation(
        self,
    ) -> None:
        before_canonical = self.fixture.dirty_canonical()
        before_reflog = self.fixture.remote_reflog_count()
        environment = {
            **os.environ,
            "CODEX_GIT_FINALIZER_TEST_CRASH_AFTER_PUSH": "1",
        }
        interrupted = subprocess.run(
            self.fixture.command(),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(interrupted.returncode, 97)
        self.assertEqual(
            git(self.fixture.remote, "rev-parse", "refs/heads/main"),
            self.fixture.candidate_oid,
        )
        self.assertEqual(self.fixture.remote_reflog_count(), before_reflog + 1)

        recovered = subprocess.run(
            self.fixture.command(), check=False, capture_output=True, text=True
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        summary = json.loads(recovered.stdout)
        self.assertEqual(summary["push"]["result"], "already_published_recovered")
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(summary["remote_verify"]["oid"], self.fixture.candidate_oid)
        self.assertEqual(self.fixture.remote_reflog_count(), before_reflog + 1)
        self.assertEqual(self.fixture.canonical_state(), before_canonical)

    def test_concurrent_duplicate_executor_performs_one_remote_update(self) -> None:
        before_reflog = self.fixture.remote_reflog_count()
        first = subprocess.Popen(
            self.fixture.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            self.fixture.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_output, first_error = first.communicate(timeout=20)
        second_output, second_error = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(second.returncode, 0, second_error)
        results = {
            json.loads(first_output)["push"]["result"],
            json.loads(second_output)["push"]["result"],
        }
        self.assertEqual(results, {"published", "already_published_recovered"})
        self.assertEqual(self.fixture.remote_reflog_count(), before_reflog + 1)

    def test_expired_writer_cannot_modify_main(self) -> None:
        self.fixture._write_metadata(datetime.now(timezone.utc) - timedelta(seconds=1))
        before_reflog = self.fixture.remote_reflog_count()
        result = subprocess.run(
            self.fixture.command(), check=False, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "blocked")
        self.assertIn("expired", summary["reason"])
        self.assertEqual(
            git(self.fixture.remote, "rev-parse", "refs/heads/main"),
            self.fixture.expected_main,
        )
        self.assertEqual(self.fixture.remote_reflog_count(), before_reflog)


if __name__ == "__main__":
    unittest.main()
