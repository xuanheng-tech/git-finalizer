"""Regression tests for CLI flag-matrix hints, structured whitespace diagnostics, and Controller retirement handshake."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from git_finalizer.delivery import retirement_plan
from git_finalizer.delivery.validation import parse_whitespace_diagnostics
from git_finalizer.errors import ErrorCode

FINALIZER = ROOT / "git-finalize"


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env.update(
        {
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Friction Tester",
            "GIT_AUTHOR_EMAIL": "friction@example.invalid",
            "GIT_COMMITTER_NAME": "Friction Tester",
            "GIT_COMMITTER_EMAIL": "friction@example.invalid",
        }
    )
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=True,
    )
    return res.stdout.strip()


class FrictionFixesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gf-friction-")
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "--initial-branch=main", str(self.remote)],
            env=_clean_env(),
            check=True,
        )
        subprocess.run(
            ["git", "init", "-q", "--initial-branch=main", str(self.repo)],
            env=_clean_env(),
            check=True,
        )
        _git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "--", "app.py")
        _git(self.repo, "commit", "-q", "-m", "seed")
        _git(self.repo, "remote", "add", "origin", str(self.remote))
        _git(self.repo, "push", "-q", "--set-upstream", "origin", "main")
        self.head = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(FINALIZER), *args],
            capture_output=True,
            text=True,
            env=_clean_env(extra_env),
            check=False,
        )

    # --- 1. CLI flag-matrix UX (preserving every safety boundary) ---

    def test_snapshot_outside_initial_publish_gives_actionable_fix_without_weakening(self) -> None:
        snap = "a" * 64
        for mode_args in (
            ("--mode", "commit-only", "--message", "m", "--", "app.py"),
            ("--initial-branch-publish", "--remote", "origin", "--remote-branch", "feat/x", "--message", "m", "--", "app.py"),
            ("--publish-existing-branch", self.head, "--remote", "origin", "--remote-branch", "feat/x"),
        ):
            with self.subTest(mode_args=mode_args):
                res = self._run("--summary", "--repo", str(self.repo), "--snapshot", snap, *mode_args)
                self.assertEqual(res.returncode, 1)
                doc = json.loads(res.stdout)
                self.assertEqual(doc["status"], "blocked")
                self.assertEqual(doc["final_phase"], "cli")
                self.assertEqual(doc["error_code"], ErrorCode.CLI_INVALID_ARGS)
                self.assertIn("--snapshot 只允许与 --initial-publish 一起使用", doc["reason"])
                hint = doc["diagnostics"]["cli_hint"]
                self.assertEqual(hint["issue"], "snapshot_disallowed_for_mode")
                self.assertNotIn("--snapshot", hint["suggested_command"])
                self.assertNotIn(snap, hint["suggested_command"])

    def test_missing_double_dash_and_message_after_double_dash_emit_exact_fix(self) -> None:
        res_missing = self._run(
            "--summary", "--mode", "commit-only", "--repo", str(self.repo), "-m", "msg", "app.py"
        )
        self.assertEqual(res_missing.returncode, 1)
        doc_missing = json.loads(res_missing.stdout)
        self.assertEqual(doc_missing["error_code"], ErrorCode.CLI_INVALID_ARGS)
        hint_missing = doc_missing["diagnostics"]["cli_hint"]
        self.assertEqual(hint_missing["issue"], "missing_double_dash_before_paths")
        self.assertIn(" -- app.py", hint_missing["suggested_command"])

        res_after = self._run(
            "--summary", "--mode", "commit-only", "--repo", str(self.repo), "--", "-m", "msg", "app.py"
        )
        self.assertEqual(res_after.returncode, 1)
        doc_after = json.loads(res_after.stdout)
        self.assertEqual(doc_after["error_code"], ErrorCode.CLI_INVALID_ARGS)
        hint_after = doc_after["diagnostics"]["cli_hint"]
        self.assertEqual(hint_after["issue"], "message_flag_after_double_dash")
        self.assertIn("-m msg -- app.py", hint_after["suggested_command"])

    def test_missing_message_fails_at_cli_without_staging_or_crashing(self) -> None:
        (self.repo / "app.py").write_text("x = 2\n", encoding="utf-8")
        idx_before = _git(self.repo, "ls-files", "--stage")
        res = self._run("--summary", "--mode", "commit-only", "--repo", str(self.repo), "--", "app.py")
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["final_phase"], "cli")
        self.assertEqual(doc["error_code"], ErrorCode.CLI_INVALID_ARGS)
        self.assertEqual(doc["diagnostics"]["cli_hint"]["issue"], "missing_message")
        self.assertEqual(_git(self.repo, "ls-files", "--stage"), idx_before)

    def test_publish_existing_branch_rejects_paths_and_gives_stripped_command(self) -> None:
        res = self._run(
            "--summary",
            "--publish-existing-branch",
            self.head,
            "--remote",
            "origin",
            "--remote-branch",
            "feat/x",
            "--repo",
            str(self.repo),
            "--",
            "app.py",
        )
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["final_phase"], "cli")
        self.assertEqual(doc["error_code"], ErrorCode.CLI_INVALID_ARGS)
        hint = doc["diagnostics"]["cli_hint"]
        self.assertEqual(hint["issue"], "paths_disallowed_for_mode")
        self.assertNotIn("app.py", hint["suggested_command"])

    def test_retirement_ci_flags_fail_fast_at_cli_when_missing_ci_required(self) -> None:
        res = self._run(
            "--summary",
            "--retire-remote-branch",
            "feat/x",
            "--remote",
            "origin",
            "--integrated-into",
            "main",
            "--expected-remote-oid",
            self.head,
            "--ci-status",
            "SUCCESS",
            "--repo",
            str(self.repo),
        )
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["final_phase"], "cli")
        self.assertEqual(doc["error_code"], ErrorCode.CLI_INVALID_ARGS)
        hint = doc["diagnostics"]["cli_hint"]
        self.assertEqual(hint["issue"], "ci_evidence_without_ci_required")
        self.assertIn("--ci-required", hint["suggested_command"])

        # Local retirement suggests stripping --ci-* when --ci-required was omitted.
        res_loc = self._run(
            "--summary",
            "--retire-local-branch",
            "feat/x",
            "--remote",
            "origin",
            "--integrated-into",
            "main",
            "--expected-local-oid",
            self.head,
            "--expected-integrated-oid",
            self.head,
            "--retirement-plan-id",
            "1" * 64,
            "--ci-status",
            "SUCCESS",
            "--repo",
            str(self.repo),
        )
        self.assertEqual(res_loc.returncode, 1)
        doc_loc = json.loads(res_loc.stdout)
        self.assertEqual(doc_loc["final_phase"], "cli")
        self.assertNotIn("--ci-status", doc_loc["diagnostics"]["cli_hint"]["suggested_command"])

    # --- 2. Whitespace structured diagnostics (exact path:line:reason, zero source excerpts) ---

    def test_whitespace_parser_ignores_source_line_colons_and_lists_all_violations(self) -> None:
        sample = (
            "app.py:1: trailing whitespace.\n"
            "+def foo():   \n"
            "app.py:2: trailing whitespace.\n"
            "+    payload = {'key': 'should_never_appear_in_diag'}  \n"
            "app.py:4: new blank line at EOF.\n"
            "notes.md:2: trailing whitespace.\n"
            "+line with : colon \n"
        )
        violations, unparsed = parse_whitespace_diagnostics(sample, ["app.py", "notes.md"])
        self.assertEqual(unparsed, [])
        self.assertEqual(
            violations,
            [
                {"path": "app.py", "line": 1, "reason": "trailing whitespace"},
                {"path": "app.py", "line": 2, "reason": "trailing whitespace"},
                {"path": "app.py", "line": 4, "reason": "new blank line at EOF"},
                {"path": "notes.md", "line": 2, "reason": "trailing whitespace"},
            ],
        )

    def test_whitespace_summary_reports_exact_locations_without_leaking_source_lines(self) -> None:
        (self.repo / "app.py").write_text(
            "def foo():   \n    d = {'k': 'UNIQUE_SOURCE_SENTINEL'}  \n    return d\n\n",
            encoding="utf-8",
        )
        (self.repo / "notes.md").write_text("# heading  \nbody\t\n", encoding="utf-8")

        for mode_args in (
            ("--mode", "verify-only", "--", "app.py", "notes.md"),
            ("--mode", "commit-only", "--message", "ws", "--", "app.py", "notes.md"),
        ):
            with self.subTest(mode_args=mode_args):
                res = self._run("--summary", "--repo", str(self.repo), *mode_args)
                self.assertEqual(res.returncode, 1)
                self.assertNotIn("UNIQUE_SOURCE_SENTINEL", res.stdout)
                doc = json.loads(res.stdout)
                self.assertEqual(doc["status"], "blocked")
                self.assertEqual(doc["final_phase"], "local_validation")
                self.assertEqual(doc["error_code"], ErrorCode.WHITESPACE_VIOLATION)
                self.assertIn("app.py:1:trailing whitespace", doc["reason"])
                self.assertIn("notes.md:1:trailing whitespace", doc["reason"])
                ws = doc["diagnostics"]["whitespace"]
                self.assertEqual(ws["files"], ["app.py", "notes.md"])
                self.assertEqual(ws["total_count"], 5)
                self.assertEqual(ws["omitted_count"], 0)
                locs = [(v["path"], v["line"], v["reason"]) for v in ws["violations"]]
                self.assertEqual(
                    locs,
                    [
                        ("app.py", 1, "trailing whitespace"),
                        ("app.py", 2, "trailing whitespace"),
                        ("app.py", 4, "new blank line at EOF"),
                        ("notes.md", 1, "trailing whitespace"),
                        ("notes.md", 2, "trailing whitespace"),
                    ],
                )

    def test_commit_range_whitespace_reports_exact_locations_and_commit(self) -> None:
        (self.repo / "app.py").write_text("def bar():  \n    return 1\n", encoding="utf-8")
        _git(self.repo, "add", "--", "app.py")
        _git(self.repo, "commit", "-q", "-m", "bad ws commit")
        bad_head = _git(self.repo, "rev-parse", "HEAD")

        res = self._run("--summary", "--resume-publish", bad_head, "--repo", str(self.repo))
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["error_code"], ErrorCode.WHITESPACE_VIOLATION)
        ws = doc["diagnostics"]["whitespace"]
        self.assertEqual(ws["total_count"], 1)
        self.assertEqual(
            ws["violations"][0],
            {"path": "app.py", "line": 1, "reason": "trailing whitespace", "commit": bad_head[:12]},
        )

    # --- 3. Controller <-> Finalizer retirement-plan handshake ---

    def test_partial_drifted_and_retirement_stale_report_replan_command(self) -> None:
        args = argparse.Namespace(
            repo=self.repo,
            plan_id="1" * 64,
            operation="local",
            branch="feat/retire",
            remote="origin",
            integrated_into="main",
            expected_oid=self.head,
            expected_integrated_oid=self.head,
            recover=True,
        )
        partial_drifted = {
            "code": "BRANCH_RETIREMENT_PARTIAL_DRIFTED",
            "completed_operations": ["remote"],
            "current": {
                "schema_version": 1,
                "plan_id": "9" * 64,
                "branch": "feat/retire",
                "remote": "origin",
                "expected_oid": self.head,
                "integrated_ref": "refs/remotes/origin/main",
                "expected_integrated_oid": "2" * 40,
                "blockers": ["PLAN_STATE_DRIFT"],
            },
        }
        with (
            mock.patch.object(retirement_plan, "controller_call", return_value=partial_drifted),
            self.assertRaises(retirement_plan.RetirementBlocked) as ctx,
        ):
            retirement_plan.preflight(args)
        self.assertIn("plan is stale; create a fresh plan", str(ctx.exception))
        self.assertIn("BRANCH_RETIREMENT_PARTIAL_DRIFTED", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.details)
        assert ctx.exception.details is not None
        self.assertEqual(ctx.exception.details["controller_code"], "BRANCH_RETIREMENT_PARTIAL_DRIFTED")
        self.assertIn("worktree-controller branch-retirement-plan", ctx.exception.details["replan_command"])

        # Verify stage returning RETIREMENT_STALE also reports stale plan + replan_command.
        status_ok = {
            "code": "BRANCH_RETIREMENT_READY",
            "completed_operations": [],
            "current": {
                "schema_version": 1,
                "plan_id": "1" * 64,
                "branch": "feat/retire",
                "remote": "origin",
                "expected_oid": self.head,
                "integrated_ref": "refs/remotes/origin/main",
                "expected_integrated_oid": self.head,
                "blockers": [],
            },
        }
        verify_stale = {
            "code": "RETIREMENT_STALE",
            "blockers": ["PLAN_STATE_DRIFT"],
        }
        with (
            mock.patch.object(retirement_plan, "controller_call", side_effect=[status_ok, verify_stale]),
            self.assertRaises(retirement_plan.RetirementBlocked) as ctx2,
        ):
            retirement_plan.preflight(args)
        self.assertIn("plan is stale; create a fresh plan", str(ctx2.exception))
        self.assertIn("RETIREMENT_STALE", str(ctx2.exception))

    def test_identity_mismatch_names_exact_fields_and_unknown_plan_surfaces_code_safely(self) -> None:
        args = argparse.Namespace(
            repo=self.repo,
            plan_id="1" * 64,
            operation="local",
            branch="feat/retire",
            remote="origin",
            integrated_into="main",
            expected_oid=self.head,
            expected_integrated_oid=self.head,
            recover=True,
        )
        doc = {
            "schema_version": 1,
            "plan_id": "1" * 64,
            "branch": "feat/other",
            "remote": "origin",
            "expected_oid": "3" * 40,
            "integrated_ref": "refs/remotes/origin/main",
            "expected_integrated_oid": self.head,
        }
        with self.assertRaises(retirement_plan.RetirementBlocked) as ctx:
            retirement_plan.identity_matches(args, doc, status_only=True)
        self.assertIn("Controller retirement identity differs", str(ctx.exception))
        self.assertIn("branch,expected_oid", str(ctx.exception))
        assert ctx.exception.details is not None
        self.assertEqual(ctx.exception.details["mismatched_fields"], ["branch", "expected_oid"])

        # Controller JSON error extracts ONLY uppercase code token, never message prose.
        controller_err = subprocess.CompletedProcess(
            [],
            1,
            json.dumps(
                {
                    "errors": [
                        {
                            "code": "UNKNOWN_RETIREMENT_PLAN",
                            "message": "SECRET_PROSE_MUST_NOT_LEAK",
                        }
                    ]
                }
            ),
            "STDERR_MUST_NOT_LEAK",
        )
        with (
            mock.patch("shutil.which", return_value="fake-wc"),
            mock.patch("subprocess.run", return_value=controller_err),
            self.assertRaises(retirement_plan.RetirementBlocked) as ctx_err,
        ):
            retirement_plan.preflight(args)
        msg = str(ctx_err.exception)
        self.assertIn("UNKNOWN_RETIREMENT_PLAN", msg)
        self.assertIn("worktree-controller branch-retirement-plan", msg)
        self.assertNotIn("SECRET_PROSE_MUST_NOT_LEAK", msg)
        self.assertNotIn("STDERR_MUST_NOT_LEAK", msg)

    # --- 4. Skill & README hygiene ---

    def test_skill_has_no_routine_context_loader_directives_and_readme_reflects_pypi(self) -> None:
        skill_md = (ROOT / "skills" / "git-change-delivery" / "SKILL.md").read_text(encoding="utf-8")
        ctx_ref = (ROOT / "skills" / "git-change-delivery" / "references" / "context-loader.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("需要确定性上下文时用 Context Loader", skill_md)
        self.assertIn("已冻结", skill_md)
        self.assertIn("维护冻结", ctx_ref)
        self.assertNotIn("There is no package index", readme)
        self.assertIn("https://pypi.org/project/git-finalizer/", readme)


if __name__ == "__main__":
    unittest.main()
