from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import unpublished_queue as queue


NOW_1 = "2026-08-09T12:00:00+00:00"
NOW_2 = "2026-08-09T12:05:00+00:00"
OID_A = "a" * 40


class QueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self._init_repo(self.repo)
        (self.repo / "task.txt").write_text("task result\n", encoding="utf-8")

    def _git(self, repo: Path, *arguments: str) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Queue Fixture",
                "GIT_AUTHOR_EMAIL": "queue@example.invalid",
                "GIT_COMMITTER_NAME": "Queue Fixture",
                "GIT_COMMITTER_EMAIL": "queue@example.invalid",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            self.fail(completed.stderr.decode("utf-8", errors="replace"))
        return completed.stdout.decode("utf-8").strip()

    def _init_repo(self, path: Path) -> None:
        path.mkdir()
        self._git(path, "init", "-b", "main")
        (path / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git(path, "add", "README.md")
        self._git(path, "commit", "-m", "test: initialize fixture")

    def _upsert(
        self,
        *,
        repo: Path | None = None,
        task_id: str = "turn-1",
        thread_id: str = "thread-1",
        workstream: str = "fixture workstream",
        decision: str = "intentionally_unpublished",
        scope: str = "not_applicable",
        outcome: str = "not_run",
        paths: tuple[str, ...] = ("task.txt",),
        now: str = NOW_1,
    ) -> tuple[str, dict[str, object] | None]:
        return queue.upsert(
            state_dir=self.state,
            repo=repo or self.repo,
            task_id=task_id,
            thread_id=thread_id,
            workstream=workstream,
            decision=decision,
            scope=scope,
            outcome=outcome,
            paths=paths,
            now=now,
        )

    def _records(self) -> dict[str, dict[str, object]]:
        with queue._queue_lock(self.state, create=False, exclusive=False):
            return queue._load_records_locked(self.state)

    def test_intentionally_unpublished_creates_pending(self) -> None:
        action, record = self._upsert()

        self.assertEqual(action, "created")
        assert record is not None
        self.assertEqual(record["queue_state"], "pending")
        self.assertEqual(queue.validate_queue(self.state), (1, 1))

    def test_publication_blocked_and_publish_now_blocked_create_blocked(self) -> None:
        _, first = self._upsert(
            task_id="turn-blocked",
            workstream="preflight blocked",
            decision="publication_blocked",
            scope="commit_and_push",
            outcome="blocked",
        )
        _, second = self._upsert(
            task_id="turn-publish-blocked",
            workstream="execution blocked",
            decision="publish_now",
            scope="commit_and_push",
            outcome="blocked",
        )

        assert first is not None and second is not None
        self.assertEqual(first["queue_state"], "blocked")
        self.assertEqual(second["queue_state"], "blocked")

    def test_remote_pushed_without_verification_remains_blocked(self) -> None:
        _, record = self._upsert(
            decision="publish_now",
            scope="commit_and_push",
            outcome="remote_pushed",
        )

        assert record is not None
        self.assertEqual(record["queue_state"], "blocked")

    def test_remote_verified_does_not_create_active_record(self) -> None:
        action, record = queue.upsert(
            state_dir=self.state,
            repo=self.repo,
            task_id="turn-verified",
            thread_id="thread-1",
            workstream="verified workstream",
            decision="publish_now",
            scope="commit_and_push",
            outcome="remote_verified",
            paths=(),
            remote_verified_oid=OID_A,
            now=NOW_1,
        )

        self.assertEqual(action, "no_active_record")
        self.assertIsNone(record)
        self.assertEqual(queue.validate_queue(self.state), (0, 0))

    def test_duplicate_upsert_is_idempotent_and_scope_expansion_updates(self) -> None:
        action, first = self._upsert()
        repeated_action, repeated = self._upsert(now=NOW_2)
        (self.repo / "second.txt").write_text("second\n", encoding="utf-8")
        updated_action, updated = self._upsert(
            paths=("task.txt", "second.txt"), now=NOW_2
        )

        self.assertEqual(action, "created")
        self.assertEqual(repeated_action, "unchanged")
        self.assertEqual(updated_action, "updated")
        assert first is not None and repeated is not None and updated is not None
        self.assertEqual(first["record_id"], repeated["record_id"])
        self.assertEqual(first["record_id"], updated["record_id"])
        self.assertEqual(updated["path_scope"]["path_count"], 2)
        self.assertEqual(len(self._records()), 1)

    def test_path_scope_is_bounded(self) -> None:
        paths = tuple(f"generated/file-{index:02d}.txt" for index in range(20))
        _, record = self._upsert(paths=paths)

        assert record is not None
        path_scope = record["path_scope"]
        self.assertEqual(path_scope["kind"], "bounded")
        self.assertEqual(path_scope["path_count"], 20)
        self.assertEqual(len(path_scope["items"]), queue.MAX_SCOPE_PATHS)
        self.assertEqual(path_scope["paths_omitted"], 4)

    def test_record_over_8_kib_fails_without_writing(self) -> None:
        paths: list[str] = []
        for index in range(queue.MAX_SCOPE_PATHS):
            relative = "/".join(
                (
                    f"segment-{index:02d}-" + "a" * 90,
                    "b" * 100,
                    "c" * 100 + f"-{index:02d}.txt",
                )
            )
            target = self.repo / relative
            target.parent.mkdir(parents=True)
            target.write_text("x\n", encoding="utf-8")
            paths.append(relative)

        with self.assertRaisesRegex(queue.QueueError, "exceeds 8192 bytes"):
            self._upsert(paths=tuple(paths))
        self.assertEqual(queue.validate_queue(self.state), (0, 0))

    def test_summary_is_repo_scoped_and_empty_without_active_records(self) -> None:
        other = self.root / "other"
        self._init_repo(other)
        (other / "other.txt").write_text("other\n", encoding="utf-8")

        self.assertEqual(queue.summary(self.state, self.repo), "")
        self._upsert(repo=other, paths=("other.txt",), workstream="other repo")
        self.assertEqual(queue.summary(self.state, self.repo), "")
        self._upsert()

        text = queue.summary(self.state, self.repo)
        self.assertEqual(
            text,
            "Unpublished backlog: 1 pending, 0 blocked. "
            "Oldest: 2026-08-09. Run queue review for details.",
        )
        self.assertLessEqual(len(text), 300)

    def test_published_record_is_not_in_active_summary(self) -> None:
        _, record = self._upsert()
        assert record is not None
        queue.close(
            state_dir=self.state,
            record_id=str(record["record_id"]),
            state="published",
            evidence_kind="remote_verified",
            oid=OID_A,
            now=NOW_2,
        )

        self.assertEqual(queue.summary(self.state, self.repo), "")
        self.assertEqual(queue.validate_queue(self.state), (1, 0))

    def test_factor_miner_equivalent_closes_behind_worktree_without_changes(self) -> None:
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            stdout=subprocess.PIPE,
        )
        self._git(self.repo, "remote", "add", "origin", str(remote))
        self._git(self.repo, "push", "-u", "origin", "main")

        publisher = self.root / "publisher"
        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote), str(publisher)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        report_path = "docs/audit/standalone.md"
        published_report = publisher / report_path
        published_report.parent.mkdir(parents=True)
        published_report.write_text("verified audit\n", encoding="utf-8")
        self._git(publisher, "add", report_path)
        self._git(publisher, "commit", "-m", "docs: publish standalone audit")
        self._git(publisher, "push", "origin", "main")
        remote_oid = self._git(publisher, "rev-parse", "HEAD")

        self._git(self.repo, "fetch", "origin", "main")
        local_report = self.repo / report_path
        local_report.parent.mkdir(parents=True)
        local_report.write_text("verified audit\n", encoding="utf-8")
        (self.repo / "staged.txt").write_text("preserve staged\n", encoding="utf-8")
        self._git(self.repo, "add", "staged.txt")
        _, record = self._upsert(paths=(report_path,), workstream="standalone audit")
        assert record is not None
        status_before = self._git(self.repo, "status", "--porcelain=v1")
        index_before = hashlib.sha256(
            subprocess.run(
                ["git", "-C", str(self.repo), "diff", "--cached", "--binary"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
        ).hexdigest()

        results = queue.review(
            state_dir=self.state,
            repo=self.repo,
            record_id=str(record["record_id"]),
            remote_verified_oid=remote_oid,
            now=NOW_2,
        )

        self.assertEqual(results[0]["result"], "already_published_equivalent")
        self.assertEqual(results[0]["queue_state"], "published")
        self.assertEqual(results[0]["behind"], 1)
        closed = self._records()[str(record["record_id"])]
        self.assertEqual(
            closed["closure_evidence"]["kind"], "already_published_equivalent"
        )
        self.assertEqual(closed["closure_evidence"]["oid"], remote_oid)
        self.assertEqual(self._git(self.repo, "status", "--porcelain=v1"), status_before)
        index_after = hashlib.sha256(
            subprocess.run(
                ["git", "-C", str(self.repo), "diff", "--cached", "--binary"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
        ).hexdigest()
        self.assertEqual(index_after, index_before)

    def test_equivalent_without_remote_evidence_needs_human_review(self) -> None:
        remote = self.root / "equivalent.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            stdout=subprocess.PIPE,
        )
        self._git(self.repo, "remote", "add", "origin", str(remote))
        self._git(self.repo, "push", "-u", "origin", "main")
        self._git(self.repo, "add", "task.txt")
        self._git(self.repo, "commit", "-m", "test: publish equivalent content")
        self._git(self.repo, "push", "origin", "main")
        _, record = self._upsert()
        assert record is not None

        results = queue.review(
            state_dir=self.state,
            repo=self.repo,
            record_id=str(record["record_id"]),
            now=NOW_2,
        )

        self.assertEqual(results[0]["result"], "needs_human_review")
        self.assertEqual(self._records()[str(record["record_id"])]["queue_state"], "pending")

    def test_review_classifies_pending_blocked_and_superseded_candidate(self) -> None:
        remote = self.root / "classifications.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            stdout=subprocess.PIPE,
        )
        self._git(self.repo, "remote", "add", "origin", str(remote))
        self._git(self.repo, "push", "-u", "origin", "main")
        (self.repo / "blocked.txt").write_text("blocked\n", encoding="utf-8")
        (self.repo / "superseded.txt").write_text("original\n", encoding="utf-8")
        _, pending = self._upsert(task_id="pending", workstream="pending")
        _, blocked = self._upsert(
            task_id="blocked",
            workstream="blocked",
            decision="publication_blocked",
            scope="commit_and_push",
            outcome="blocked",
            paths=("blocked.txt",),
        )
        _, superseded = self._upsert(
            task_id="superseded",
            workstream="superseded",
            paths=("superseded.txt",),
        )
        (self.repo / "superseded.txt").write_text("later change\n", encoding="utf-8")
        assert pending is not None and blocked is not None and superseded is not None

        results = queue.review(state_dir=self.state, repo=self.repo, now=NOW_2)
        by_id = {result["record_id"]: result["result"] for result in results}

        self.assertEqual(by_id[pending["record_id"]], "still_pending")
        self.assertEqual(by_id[blocked["record_id"]], "still_blocked")
        self.assertEqual(by_id[superseded["record_id"]], "superseded_candidate")

    def test_invalid_jsonl_and_schema_fail_closed(self) -> None:
        self._upsert()
        queue_path = self.state / "queue.jsonl"
        queue_path.write_text("not-json\n", encoding="utf-8")
        queue_path.chmod(0o600)

        with self.assertRaisesRegex(queue.QueueError, "invalid JSON"):
            queue.validate_queue(self.state)

        invalid = {"schema_version": queue.SCHEMA_VERSION}
        queue_path.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
        queue_path.chmod(0o600)
        with self.assertRaisesRegex(queue.QueueError, "unexpected fields"):
            queue.validate_queue(self.state)

    def test_concurrent_cli_upserts_are_atomic(self) -> None:
        helper = Path(queue.__file__).resolve()
        commands = []
        for index in range(8):
            commands.append(
                [
                    sys.executable,
                    "-B",
                    str(helper),
                    "--state-dir",
                    str(self.state),
                    "upsert",
                    "--repo",
                    str(self.repo),
                    "--task-id",
                    f"concurrent-turn-{index}",
                    "--thread-id",
                    "concurrent-thread",
                    "--workstream",
                    f"concurrent workstream {index}",
                    "--publication-decision",
                    "intentionally_unpublished",
                    "--finalization-scope",
                    "not_applicable",
                    "--finalization-outcome",
                    "not_run",
                    "--path",
                    "task.txt",
                ]
            )
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for command in commands
        ]
        results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        for stdout, stderr, returncode in results:
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assertEqual(returncode, 0, stderr.decode("utf-8", errors="replace"))

        self.assertEqual(queue.validate_queue(self.state), (8, 8))

    def test_migration_dry_run_is_deterministic_and_does_not_import(self) -> None:
        source = self.root / "migration.json"
        payload = {
            "schema_version": queue.MIGRATION_SOURCE_SCHEMA,
            "source_cutoff": "2026-08-09T20:14:26+08:00",
            "source_name": "frozen-review",
            "entries": [
                {
                    "legacy_ref": "medium-missing-identity",
                    "repo": str(self.repo),
                    "workstream": "medium candidate",
                    "proposed_state": "pending",
                    "confidence": "medium",
                    "task_id": None,
                    "thread_id": None,
                    "paths": [],
                },
                {
                    "legacy_ref": "low-candidate",
                    "repo": str(self.repo),
                    "workstream": "low candidate",
                    "proposed_state": "blocked",
                    "confidence": "low",
                    "task_id": "legacy-turn",
                    "thread_id": "legacy-thread",
                    "paths": ["task.txt"],
                },
            ],
        }
        source.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        output = self.state / "migration-dry-run.json"
        first = queue.migration_dry_run(
            state_dir=self.state, source=source, output=output
        )
        first_bytes = output.read_bytes()
        second = queue.migration_dry_run(
            state_dir=self.state, source=source, output=output
        )

        self.assertEqual(first, second)
        self.assertEqual(first["candidate_count"], 2)
        self.assertEqual(first["eligible_import_count"], 0)
        self.assertEqual(first["human_confirmation_count"], 1)
        self.assertEqual(first["excluded_count"], 1)
        self.assertFalse(first["formal_import_performed"])
        self.assertEqual(queue.validate_queue(self.state), (0, 0))
        self.assertEqual(output.read_bytes(), first_bytes)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_state_permissions_are_private(self) -> None:
        self._upsert()

        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for name in ("key", "queue.lock", "queue.jsonl"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)

    def test_current_task_resolution_uses_only_unmatched_lifecycle(self) -> None:
        codex_home = self.root / "codex"
        sessions = codex_home / "sessions" / "2026" / "08" / "09"
        sessions.mkdir(parents=True)
        thread = "11111111-1111-4111-8111-111111111111"
        rollout = sessions / f"rollout-fixture-{thread}.jsonl"
        events = (
            {"type": "session_meta", "payload": {"id": thread}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "old"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "current"}},
        )
        rollout.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        self.assertEqual(queue.resolve_current_task_id(codex_home, thread), "current")


if __name__ == "__main__":
    unittest.main()
