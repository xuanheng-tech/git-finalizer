from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import deploy
import quick_validate


VERSIONED_SOURCE = Path(__file__).resolve().parent


def render_finalization_report(
    decision: str,
    scope: str,
    outcome: str,
    *body: str,
) -> str:
    lines = [
        *body,
        f"Publication decision: {decision}",
        f"Finalization scope: {scope}",
        f"Finalization outcome: {outcome}",
    ]
    return "\n".join(lines) + "\n"


class SkillDeploymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source" / "git-change-delivery"
        shutil.copytree(VERSIONED_SOURCE, self.source)
        self.compatibility_source = self.root / "source" / "three-tool-git-workflow"
        shutil.copytree(VERSIONED_SOURCE.parent / "three-tool-git-workflow", self.compatibility_source)
        for path in self.source.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        self.skills_root = self.root / ".agents" / "skills"
        self.skills_root.mkdir(parents=True)
        self.live = self.skills_root / "git-change-delivery"
        self.compatibility_live = self.skills_root / "three-tool-git-workflow"

    def run_main(self, action: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = deploy.main(
                (
                    action,
                    "--source-dir",
                    str(self.source),
                    "--live-dir",
                    str(self.live),
                    "--compatibility-source-dir",
                    str(self.compatibility_source),
                    "--compatibility-live-dir",
                    str(self.compatibility_live),
                )
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def populate_live(self) -> None:
        self.live.mkdir(mode=0o700)
        (self.live / "references").mkdir(mode=0o700)
        for name in deploy.MANAGED_FILES:
            target = self.live / name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            target.write_bytes((self.source / name).read_bytes())
            target.chmod(0o600)
        self.compatibility_live.mkdir(mode=0o700)
        target = self.compatibility_live / "SKILL.md"
        target.write_bytes((self.compatibility_source / "SKILL.md").read_bytes())
        target.chmod(0o600)

    def materialize_released_live(self, ref: str) -> bool:
        """Populate the live tree with the payload a released tag actually carried."""
        repository = VERSIONED_SOURCE.parents[1]
        blobs = {}
        for name in deploy.MANAGED_FILES:
            result = subprocess.run(
                ["git", "show", f"{ref}:skills/git-change-delivery/{name}"],
                cwd=repository,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                return False
            blobs[name] = result.stdout
        self.live.mkdir(mode=0o700)
        (self.live / "references").mkdir(mode=0o700)
        for name, content in blobs.items():
            target = self.live / name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o600)
        self.compatibility_live.mkdir(mode=0o700)
        shim = self.compatibility_live / "SKILL.md"
        shim.write_bytes((self.compatibility_source / "SKILL.md").read_bytes())
        shim.chmod(0o600)
        return True

    def test_install_upgrades_from_the_released_production_lineage(self) -> None:
        if not self.materialize_released_live("v1.3.0"):
            self.skipTest("the released v1.3.0 Skill tree is absent here")
        live_tree = deploy.payload_tree_digest(self.live)
        self.assertEqual(
            live_tree,
            "9c05e5b279731a37b3ce15e2fbda7ae9962f81355cbafb86fa410f28ec19f527",
        )
        self.assertIn(live_tree, deploy.RELEASED_CANONICAL_SKILL_SHA256)
        source_tree = deploy.payload_tree_digest(self.source)
        self.assertNotEqual(live_tree, source_tree)

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(deploy.payload_tree_digest(self.live), source_tree)
        self.assertTrue(
            any(line.startswith("installed ") for line in stdout.splitlines()),
            stdout,
        )

    def test_install_refuses_hand_drift_on_top_of_released_lineage(self) -> None:
        if not self.materialize_released_live("v1.3.0"):
            self.skipTest("the released v1.3.0 Skill tree is absent here")
        hand_edit = self.live / "SKILL.md"
        hand_edit.write_bytes(hand_edit.read_bytes() + b"\nlocally added policy\n")
        before = {
            name: deploy.sha256(self.live / name)
            for name in deploy.MANAGED_FILES
        }

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 2, stdout)
        self.assertIn("unknown content drift", stderr)
        self.assertNotIn(
            deploy.payload_tree_digest(self.live),
            deploy.RELEASED_CANONICAL_SKILL_SHA256,
        )
        after = {
            name: deploy.sha256(self.live / name)
            for name in deploy.MANAGED_FILES
        }
        self.assertEqual(after, before)


    def test_check_reports_matching_payload_hashes_and_permissions(self) -> None:
        self.populate_live()

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            sum(line.startswith("ok ") for line in stdout.splitlines()),
            len(deploy.MANAGED_FILES),
        )
        self.assertIn("compatibility-ok SKILL.md", stdout)
        self.assertNotIn("drift ", stdout)
        self.assertNotIn("unknown-live ", stdout)

    def test_check_detects_content_and_permission_drift(self) -> None:
        self.populate_live()
        target = self.live / "SKILL.md"
        target.write_bytes(b"drifted\n")
        target.chmod(0o644)

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 1)
        self.assertEqual(stderr, "")
        self.assertIn("drift SKILL.md", stdout)

    def test_install_creates_clean_live_from_scratch(self) -> None:
        other_skill = self.skills_root / "other-skill" / "SKILL.md"
        other_skill.parent.mkdir()
        other_skill.write_bytes(b"preserve exactly\n")
        other_before = hashlib.sha256(other_skill.read_bytes()).hexdigest()

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.live / "references").stat().st_mode), 0o700
        )
        for name in deploy.MANAGED_FILES:
            target = self.live / name
            self.assertEqual(deploy.sha256(target), deploy.sha256(self.source / name))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(other_skill.read_bytes()).hexdigest(), other_before)
        self.assertEqual(
            deploy.sha256(self.compatibility_live / "SKILL.md"),
            deploy.sha256(self.compatibility_source / "SKILL.md"),
        )
        self.assertFalse(
            any(path.name.startswith(".") for path in self.live.rglob("*"))
        )

    def test_install_is_idempotent_when_live_matches_source(self) -> None:
        self.populate_live()

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            sum(line.startswith("unchanged ") for line in stdout.splitlines()),
            len(deploy.MANAGED_FILES) + 1,
        )

    def test_failed_upgrade_restores_the_complete_pair_and_can_retry(self) -> None:
        self.populate_live()
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                  for root in (self.live, self.compatibility_live)
                  for path in root.rglob("*") if path.is_file()}
        previous_tree = deploy.payload_tree_digest(self.live)
        for name in ("SKILL.md", "references/git-finalizer.md"):
            with (self.source / name).open("a") as stream:
                stream.write("\nSynthetic documented upgrade.\n")
        original = deploy.install_one
        calls = 0
        def fail_third(*args):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("synthetic third-file install failure")
            return original(*args)
        with mock.patch.object(deploy, "RELEASED_CANONICAL_SKILL_SHA256", frozenset({previous_tree})):
            with mock.patch.object(deploy, "install_one", side_effect=fail_third):
                status, _stdout, stderr = self.run_main("install")
            self.assertEqual(status, 2, stderr)
            for path, expected in before.items():
                self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), expected)
            status, _stdout, stderr = self.run_main("install")
            self.assertEqual(status, 0, stderr)

    def test_failed_first_install_removes_partial_payloads(self) -> None:
        with mock.patch.object(deploy, "install_one", side_effect=OSError("synthetic write failure")):
            status, _stdout, stderr = self.run_main("install")
        self.assertEqual(status, 2, stderr)
        self.assertFalse(self.live.exists())
        self.assertFalse(self.compatibility_live.exists())

    def test_restore_failure_preserves_the_verified_backup(self) -> None:
        self.populate_live()
        with mock.patch.object(deploy, "install_one", side_effect=OSError("synthetic install failure")), \
             mock.patch.object(deploy.sync, "restore_production_pair", side_effect=OSError("synthetic restore failure")):
            status, _stdout, stderr = self.run_main("install")
        self.assertEqual(status, 2, stderr)
        recovery = list(self.skills_root.glob(".skill-install-*.recovery"))
        self.assertEqual(len(recovery), 1)
        receipt = list((recovery[0] / "backups").iterdir())
        self.assertEqual(len(receipt), 1)
        verified = deploy.sync.verify_production_backup(receipt[0])
        self.assertEqual(len(verified["entries"]), len(deploy.MANAGED_FILES) + 1)

    def test_standalone_install_waits_for_the_shared_sync_transaction_lock(self) -> None:
        command = [sys.executable, "-B", str(VERSIONED_SOURCE / "deploy.py"), "install",
                   "--source-dir", str(self.source), "--live-dir", str(self.live),
                   "--compatibility-source-dir", str(self.compatibility_source),
                   "--compatibility-live-dir", str(self.compatibility_live)]
        with deploy.sync.deployment_lock(self.skills_root):
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.3)
            self.assertFalse(self.live.exists())
        _stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(deploy.check(self.source, self.live, self.compatibility_source, self.compatibility_live))

    def test_install_upgrades_live_at_released_canonical_tree(self) -> None:
        self.populate_live()
        previous_tree = deploy.payload_tree_digest(self.live)
        target = self.source / "references" / "unpublished-queue.md"
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\nCanonical upgrade fixture line.\n")
        original_accepted = deploy.RELEASED_CANONICAL_SKILL_SHA256
        deploy.RELEASED_CANONICAL_SKILL_SHA256 = frozenset({previous_tree})
        try:
            status, stdout, stderr = self.run_main("install")
        finally:
            deploy.RELEASED_CANONICAL_SKILL_SHA256 = original_accepted

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("installed unpublished-queue.md", stdout)
        for name in deploy.MANAGED_FILES:
            self.assertEqual(
                deploy.sha256(self.live / name), deploy.sha256(self.source / name)
            )

    def test_install_refuses_unknown_drifted_live_without_changes(self) -> None:
        self.populate_live()
        target = self.live / "SKILL.md"
        target.write_bytes(b"# locally hand-enriched policy\n")
        other_skill = self.skills_root / "other-skill" / "SKILL.md"
        other_skill.parent.mkdir()
        other_skill.write_bytes(b"preserve exactly\n")
        before = {
            path.relative_to(self.live).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.live.rglob("*")
            if path.is_file()
        }

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("unknown content drift", stderr)
        self.assertIn("live=", stderr)
        after = {
            path.relative_to(self.live).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.live.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(other_skill.read_bytes(), b"preserve exactly\n")

    def test_install_refuses_incomplete_live_without_changes(self) -> None:
        self.populate_live()
        (self.live / "references" / "context-loader.md").unlink()
        before = {
            path.relative_to(self.live).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.live.rglob("*")
            if path.is_file()
        }

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("incomplete", stderr)
        after = {
            path.relative_to(self.live).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.live.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_install_refuses_unknown_live_file_without_changes(self) -> None:
        self.populate_live()
        unknown = self.live / "local-notes.md"
        unknown.write_bytes(b"preserve\n")
        before = {
            path.relative_to(self.live).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.live.rglob("*")
            if path.is_file()
        }

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("unknown paths", stderr)
        after = {
            path.relative_to(self.live).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.live.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_install_refuses_symlinked_managed_directory_before_changes(self) -> None:
        self.live.mkdir(mode=0o755)
        external = self.root / "external-references"
        external.mkdir()
        (self.live / "references").symlink_to(external, target_is_directory=True)
        root_mode_before = stat.S_IMODE(self.live.stat().st_mode)

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("not a regular directory", stderr)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), root_mode_before)
        self.assertEqual(list(external.iterdir()), [])

    def test_incomplete_source_is_rejected(self) -> None:
        (self.source / "references" / "snapshot-runner.md").unlink()

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("versioned source is not a regular file", stderr)

    def test_compatibility_source_cannot_copy_canonical_payload(self) -> None:
        self.populate_live()
        (self.compatibility_source / "references").mkdir()
        (self.compatibility_source / "references" / "copied.md").write_text(
            "duplicate\n", encoding="utf-8"
        )

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 2)
        self.assertIn("source-files", stdout)
        self.assertIn("must contain only SKILL.md", stderr)


class SkillContractTest(unittest.TestCase):
    def test_current_contract_is_valid(self) -> None:
        self.assertEqual(quick_validate.validation_errors(VERSIONED_SOURCE), [])

    def test_order_and_report_line_are_mandatory(self) -> None:
        content = (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        section = quick_validate.contract_section(content)

        self.assertEqual(
            tuple(re.findall(r"^\d+\. `([^`]+)`：", section, re.MULTILINE)),
            quick_validate.REQUIRED_STATES,
        )
        for marker in (
            "Publication decision: <publish_now|publication_blocked|intentionally_unpublished|not_applicable>",
            "Finalization scope: <commit_only|commit_and_push|not_applicable>",
            "Finalization outcome: <not_run|local_commit_created|remote_pushed|remote_verified|blocked>",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, section)

    def test_automatic_delivery_requires_user_grant_and_narrower_limits(self) -> None:
        content = (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        section = quick_validate.contract_section(content)
        self.assertIn("自动交付授权覆盖的仓库开发任务", section)
        self.assertIn("遵守这次更窄指令", section)
        self.assertIn("没有适用持续授权时才补充", section)
        self.assertNotIn("保持 explicit-only", section)
        self.assertNotIn("一般实现、测试或验收要求都不构成 commit/push 授权", section)

    def test_historical_and_edge_scenarios(self) -> None:
        cases = {
            "read_only": render_finalization_report(
                "not_applicable", "not_applicable", "not_run"
            ),
            "changed_without_authorization": render_finalization_report(
                "intentionally_unpublished", "not_applicable", "not_run"
            ),
            "user_explicitly_forbids_publication": render_finalization_report(
                "intentionally_unpublished", "not_applicable", "not_run"
            ),
            "commit_only_success": render_finalization_report(
                "publish_now",
                "commit_only",
                "local_commit_created",
                "Local commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ),
            "commit_and_push_verified": render_finalization_report(
                "publish_now", "commit_and_push", "remote_verified"
            ),
            "push_without_remote_verification": render_finalization_report(
                "publish_now", "commit_and_push", "remote_pushed"
            ),
            "finalizer_preflight_blocked": render_finalization_report(
                "publication_blocked", "commit_and_push", "blocked"
            ),
            "local_commit_then_push_blocked": render_finalization_report(
                "publication_blocked",
                "commit_and_push",
                "blocked",
                "Local commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "Remote publication: blocked",
            ),
            "ambiguous_completion_instruction": render_finalization_report(
                "intentionally_unpublished", "not_applicable", "not_run"
            ),
        }
        for name, report in cases.items():
            with self.subTest(name=name):
                self.assertEqual(quick_validate.finalization_report_errors(report), [])

        commit_only = cases["commit_only_success"]
        self.assertNotIn("remote_verified", commit_only)
        self.assertNotIn("Remote publication: succeeded", commit_only)
        blocked_after_commit = cases["local_commit_then_push_blocked"]
        self.assertIn("Local commit: ", blocked_after_commit)
        self.assertIn("Remote publication: blocked", blocked_after_commit)

        section = quick_validate.contract_section(
            (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "仅要求 commit 选择 `commit-only` 且不得自行扩大为 push",
            section,
        )
        self.assertIn("工作树无法安全分离", section)
        self.assertIn("测试、验收或 Snapshot 未通过", section)

    def test_invalid_finalization_reports_are_rejected(self) -> None:
        cases = {
            "missing_decision": (
                "Finalization scope: not_applicable\n"
                "Finalization outcome: not_run\n"
            ),
            "missing_scope": (
                "Publication decision: not_applicable\n"
                "Finalization outcome: not_run\n"
            ),
            "missing_outcome": (
                "Publication decision: not_applicable\n"
                "Finalization scope: not_applicable\n"
            ),
            "duplicate_decision": (
                "Publication decision: not_applicable\n"
                "Publication decision: not_applicable\n"
                "Finalization scope: not_applicable\n"
                "Finalization outcome: not_run\n"
            ),
            "commit_only_claims_remote_verification": render_finalization_report(
                "publish_now", "commit_only", "remote_verified"
            ),
            "commit_only_claims_remote_success_in_body": render_finalization_report(
                "publish_now",
                "commit_only",
                "local_commit_created",
                "Remote publication: succeeded",
            ),
            "publish_now_did_not_run": render_finalization_report(
                "publish_now", "commit_only", "not_run"
            ),
            "unpublished_claims_current_local_commit": render_finalization_report(
                "intentionally_unpublished",
                "commit_only",
                "local_commit_created",
            ),
            "blocked_loses_local_commit_remote_fact": render_finalization_report(
                "publication_blocked",
                "commit_and_push",
                "blocked",
                "Local commit: cccccccccccccccccccccccccccccccccccccccc",
            ),
        }
        for name, report in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    quick_validate.finalization_report_errors(report), []
                )

    def test_historical_commit_only_fixture_is_normalized(self) -> None:
        expected_contract = (
            "Publication decision: publish_now\n"
            "Finalization scope: commit_only\n"
            "Finalization outcome: local_commit_created\n"
        )
        for prior_label in ("publish_now", "intentionally_unpublished"):
            with self.subTest(prior_label=prior_label):
                normalized = render_finalization_report(
                    "publish_now",
                    "commit_only",
                    "local_commit_created",
                    f"Historical decision label: {prior_label}",
                    "Explicit commit authorization: yes",
                    "Finalizer mode: commit-only",
                )
                self.assertTrue(normalized.endswith(expected_contract))
                self.assertEqual(
                    quick_validate.finalization_report_errors(normalized), []
                )

        section = quick_validate.contract_section(
            (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        )
        self.assertIn("历史 commit-only fixture", section)
        self.assertIn(
            "无论旧报告曾写 `publish_now` 还是 `intentionally_unpublished`",
            section,
        )

    def test_prior_stage_commit_exception_requires_explicit_fact(self) -> None:
        without_fact = render_finalization_report(
            "intentionally_unpublished", "commit_only", "local_commit_created"
        )
        with_fact = render_finalization_report(
            "intentionally_unpublished",
            "commit_only",
            "local_commit_created",
            "Prior-stage local commit: dddddddddddddddddddddddddddddddddddddddd",
        )

        self.assertNotEqual(
            quick_validate.finalization_report_errors(without_fact), []
        )
        self.assertEqual(quick_validate.finalization_report_errors(with_fact), [])

    def test_finalizer_mode_selection_preserves_authorization(self) -> None:
        def select_mode(*, commit: bool, push: bool, existing_commits: bool = False) -> str:
            if existing_commits and push and not commit:
                return "resume-publish"
            if push and not commit:
                return "blocked"
            if push:
                return "normal"
            if commit:
                return "commit-only"
            return "verify-only"

        self.assertEqual(select_mode(commit=False, push=False), "verify-only")
        self.assertEqual(select_mode(commit=True, push=False), "commit-only")
        self.assertEqual(select_mode(commit=True, push=True), "normal")
        self.assertEqual(select_mode(commit=False, push=True), "blocked")
        self.assertEqual(
            select_mode(commit=False, push=True, existing_commits=True),
            "resume-publish",
        )

        content = (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        for marker in quick_validate.FINALIZER_MODE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, content)

    def test_resume_publish_contract_is_explicit(self) -> None:
        skill = (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        reference = (
            VERSIONED_SOURCE / "references" / "git-finalizer.md"
        ).read_text(encoding="utf-8")

        for marker in (
            "--resume-publish <full-head-oid>",
            "configured upstream",
            "ahead >= 1",
            "behind = 0",
            "--no-follow-tags",
            "--resume-initial-publish",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)
                self.assertIn(marker, reference)
        self.assertIn("不执行 add 或 commit", reference)
        self.assertIn("remote target ref 必须等于 HEAD", reference)

    def test_unpublished_queue_contract_is_explicit_and_versioned(self) -> None:
        skill = (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        reference = (
            VERSIONED_SOURCE / "references" / "unpublished-queue.md"
        ).read_text(encoding="utf-8")

        for marker in quick_validate.QUEUE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)
        self.assertIn("current-state record", reference)
        self.assertIn("already_published_equivalent", reference)
        self.assertIn("不会正式导入", reference)
        self.assertIn("unpublished_queue.py", deploy.MANAGED_FILES)
        self.assertIn("references/unpublished-queue.md", deploy.MANAGED_FILES)


if __name__ == "__main__":
    unittest.main()
