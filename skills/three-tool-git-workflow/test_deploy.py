from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unittest

import deploy
import quick_validate


VERSIONED_SOURCE = Path(__file__).resolve().parent


class SkillDeploymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source" / "three-tool-git-workflow"
        shutil.copytree(VERSIONED_SOURCE, self.source)
        for path in self.source.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        self.skills_root = self.root / ".agents" / "skills"
        self.skills_root.mkdir(parents=True)
        self.live = self.skills_root / "three-tool-git-workflow"

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

    def test_check_reports_matching_payload_hashes_and_permissions(self) -> None:
        self.populate_live()

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("ok "), len(deploy.MANAGED_FILES))
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

    def test_install_recovers_missing_and_drifted_managed_files(self) -> None:
        self.live.mkdir(mode=0o755)
        (self.live / "references").mkdir(mode=0o755)
        (self.live / "SKILL.md").write_bytes(b"old\n")
        (self.live / "SKILL.md").chmod(0o644)
        other_skill = self.skills_root / "other-skill" / "SKILL.md"
        other_skill.parent.mkdir()
        other_skill.write_bytes(b"preserve exactly\n")
        other_before = hashlib.sha256(other_skill.read_bytes()).hexdigest()

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("installed SKILL.md", stdout)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.live / "references").stat().st_mode), 0o700
        )
        for name in deploy.MANAGED_FILES:
            target = self.live / name
            self.assertEqual(deploy.sha256(target), deploy.sha256(self.source / name))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(other_skill.read_bytes()).hexdigest(), other_before)
        self.assertFalse(
            any(path.name.startswith(".") for path in self.live.rglob("*"))
        )

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
        self.assertIn(
            "Publication decision: <publish_now|publication_blocked|intentionally_unpublished|not_applicable>",
            section,
        )

    def test_historical_and_edge_scenarios(self) -> None:
        def decide(
            *,
            changed: bool,
            blocked: bool,
            authorized: bool,
            intentionally_local: bool = False,
        ) -> str:
            if not changed:
                return "not_applicable"
            if blocked:
                return "publication_blocked"
            if intentionally_local or not authorized:
                return "intentionally_unpublished"
            return "publish_now"

        cases = {
            "normal_publish": (
                dict(changed=True, blocked=False, authorized=True),
                "publish_now",
            ),
            "cwd_blocked": (
                dict(changed=True, blocked=True, authorized=True),
                "publication_blocked",
            ),
            "user_no_publish": (
                dict(
                    changed=True,
                    blocked=False,
                    authorized=False,
                    intentionally_local=True,
                ),
                "intentionally_unpublished",
            ),
            "changed_without_authorization": (
                dict(changed=True, blocked=False, authorized=False),
                "intentionally_unpublished",
            ),
            "read_only": (
                dict(changed=False, blocked=False, authorized=False),
                "not_applicable",
            ),
            "commit_only_without_push": (
                dict(changed=True, blocked=False, authorized=True),
                "publish_now",
            ),
            "mixed_worktree": (
                dict(changed=True, blocked=True, authorized=True),
                "publication_blocked",
            ),
            "snapshot_blocked": (
                dict(changed=True, blocked=True, authorized=True),
                "publication_blocked",
            ),
        }
        for name, (arguments, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(decide(**arguments), expected)

        section = quick_validate.contract_section(
            (VERSIONED_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "仅要求 commit 选择 `commit-only` 且不得自行扩大为 push",
            section,
        )
        self.assertIn("工作树无法安全分离", section)
        self.assertIn("测试、验收或 Snapshot 未通过", section)

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


if __name__ == "__main__":
    unittest.main()
