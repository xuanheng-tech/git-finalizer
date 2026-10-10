from __future__ import annotations

import json
import os
from pathlib import Path
import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "agent-contract.md"
BOUNDARIES = ROOT / "docs" / "integration-boundaries.md"
FINALIZER = ROOT / "git-finalize"


def literals(text: str, variable: str) -> set[str]:
    return set(re.findall(rf"\b{variable}='([A-Za-z_-]+)'", text))


class AgentContractTests(unittest.TestCase):
    def scratch_repository(self, temporary: Path) -> Path:
        import subprocess

        repository = temporary / "repo"
        remote = temporary / "remote.git"
        home = temporary / "home"
        state = temporary / "state"
        for path in (repository, home, state):
            path.mkdir(parents=True, exist_ok=True)
        environment = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state),
            "GIT_CONFIG_GLOBAL": str(temporary / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": os.defpath + ":/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }

        def git(*arguments: str) -> None:
            subprocess.run(
                ["git", *arguments], cwd=repository, env=environment, check=True,
                capture_output=True,
            )

        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(remote)],
                       env=environment, check=True, capture_output=True)
        git("init", "-q", "--initial-branch=main", str(repository))
        git("config", "user.name", "contract test")
        git("config", "user.email", "contract@example.invalid")
        (repository / "f.txt").write_text("a\n", encoding="utf-8")
        git("add", "--", "f.txt")
        git("commit", "-qm", "seed")
        git("remote", "add", "origin", str(remote))
        git("push", "-q", "--set-upstream", "origin", "main")
        return repository

    def run_summary(self, repository: Path, *arguments: str) -> dict[str, object]:
        import subprocess

        environment = {
            "HOME": str(repository.parent / "home"),
            "XDG_STATE_HOME": str(repository.parent / "state"),
            "GIT_CONFIG_GLOBAL": str(repository.parent / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": os.defpath + ":/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        result = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", *arguments, "--repo", str(repository),
             "--", "f.txt"],
            capture_output=True, text=True, env=environment, check=False,
        )
        payload = result.stdout.strip() or result.stderr.strip()
        return json.loads(payload.splitlines()[-1])

    def test_nothing_to_stage_classification_is_documented(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="gf-agent-contract-") as temporary:
            repository = self.scratch_repository(Path(temporary))
            verify = self.run_summary(repository, "--mode", "verify-only")
            commit = self.run_summary(
                repository, "--mode", "commit-only", "--message", "docs: nothing to stage"
            )
            normal = self.run_summary(
                repository, "--message", "docs: nothing to stage"
            )
        expected = {
            "verify-only": ("blocked", "local_validation", "resolve_blocker_and_retry"),
            "commit-only": ("failed", "staging", "inspect_failure"),
            "normal": ("failed", "staging", "inspect_failure"),
        }
        observed = {
            "verify-only": (verify["status"], verify["final_phase"], verify["next_action"]),
            "commit-only": (commit["status"], commit["final_phase"], commit["next_action"]),
            "normal": (normal["status"], normal["final_phase"], normal["next_action"]),
        }
        self.assertEqual(observed, expected)
        for value in expected.values():
            self.assertIn(f"`{value[0]}`", self.doc)
            self.assertIn(f"`{value[1]}`", self.doc)
        self.assertIn("nothing to stage", self.doc)
        for run, label in ((verify, "verify"), (commit, "commit"), (normal, "normal")):
            self.assertIs(run["commit"]["created"], False, label)
            self.assertIs(run["push"]["executed"], False, label)

    def test_runtime_identity_is_available_before_repository_validation(self) -> None:
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory(prefix="gf-agent-runtime-") as temporary:
            repository = self.scratch_repository(Path(temporary))
            summary = self.run_summary(repository, "--diagnostics", "--mode", "unsupported-mode")
        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["final_phase"], "cli")
        self.assertEqual(summary["next_action"], "resolve_blocker_and_retry")
        self.assertIsNone(summary["diagnostics"]["content_scan"])
        for key, name in (("entrypoint", "git-finalize"),
                          ("content_scanner", "git-finalize-content-scan.py")):
            path = ROOT / name
            self.assertEqual(summary["diagnostics"]["runtime"][key], {
                "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })

    def test_diagnostics_are_optional_and_require_summary(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory(prefix="gf-agent-diagnostics-") as temporary:
            repository = self.scratch_repository(Path(temporary))
            (repository / "f.txt").write_text("candidate\n")
            plain = self.run_summary(repository, "--mode", "verify-only")
            expanded = self.run_summary(repository, "--diagnostics", "--mode", "verify-only")
            duplicate = self.run_summary(repository, "--diagnostics", "--diagnostics",
                                         "--mode", "verify-only")
            result = subprocess.run(
                [str(FINALIZER), "--diagnostics", "--repo", str(repository),
                 "--mode", "verify-only", "--", "f.txt"],
                env={"PATH": os.defpath, "HOME": str(repository.parent / "home"),
                     "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
                capture_output=True, text=True, check=False,
            )
        self.assertNotIn("diagnostics", plain)
        self.assertEqual(plain["status"], "success")
        self.assertEqual(expanded["status"], "success")
        self.assertIsNone(expanded["diagnostics"]["content_scan"])
        self.assertEqual(duplicate["status"], "blocked")
        self.assertIn("may be supplied once", duplicate["reason"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--diagnostics requires --summary", result.stderr)


    def setUp(self) -> None:
        self.source = FINALIZER.read_text(encoding="utf-8")
        self.doc = DOC.read_text(encoding="utf-8")

    def documented(self, value: str) -> bool:
        return f"`{value}`" in self.doc

    def test_status_vocabulary_is_documented(self) -> None:
        values = literals(self.source, "summary_status")
        self.assertEqual(values, {"success", "blocked", "failed"})
        for value in values:
            self.assertTrue(
                self.documented(value), f"undocumented status value: {value}"
            )

    def test_remote_conclusion_vocabulary_is_documented(self) -> None:
        values = literals(self.source, "summary_remote_conclusion")
        self.assertGreaterEqual(len(values), 25)
        for value in values:
            self.assertTrue(
                self.documented(value),
                f"undocumented remote_conclusion value: {value}",
            )

    def test_stage_verdict_vocabulary_is_documented(self) -> None:
        for variable, minimum in (
            ("summary_post_verify", 5),
            ("summary_commit_result", 3),
            ("summary_mode_state", 8),
        ):
            values = literals(self.source, variable)
            self.assertGreaterEqual(
                len(values), minimum, f"{variable} extraction collapsed"
            )
            for value in values:
                self.assertTrue(
                    self.documented(value),
                    f"undocumented {variable} value: {value}",
                )

    def test_retirement_verdict_vocabulary_is_documented(self) -> None:
        values = literals(self.source, "summary_retirement_result")
        self.assertGreaterEqual(len(values), 5)
        for value in values:
            self.assertTrue(
                self.documented(value),
                f"undocumented retirement verdict: {value}",
            )

    def test_final_phase_vocabulary_is_documented(self) -> None:
        values = literals(self.source, "summary_phase")
        self.assertGreaterEqual(len(values), 10)
        for value in values:
            self.assertTrue(
                self.documented(value), f"undocumented final_phase value: {value}"
            )

    def test_next_action_vocabulary_is_documented(self) -> None:
        values = {
            value
            for value in literals(self.source, "summary_next_action")
            if value
        }
        self.assertGreaterEqual(len(values), 14)
        for value in values:
            self.assertTrue(
                self.documented(value), f"undocumented next_action value: {value}"
            )

    def test_mode_vocabulary_is_documented(self) -> None:
        values = literals(self.source, "summary_mode")
        self.assertGreaterEqual(len(values), 11)
        self.assertIn("commit-only", values)
        for value in values:
            self.assertTrue(
                self.documented(value), f"undocumented mode value: {value}"
            )

    def test_push_result_vocabulary_is_documented(self) -> None:
        values = {
            value
            for value in re.findall(r"\bsummary_push_result='([A-Za-z_-]+)'", self.source)
        } | {
            f"skipped_by_{reason}"
            for reason in re.findall(r"\bskip_reason='([a-z_]+)'", self.source)
        }
        self.assertIn("succeeded", values)
        self.assertIn("uncertain", values)
        self.assertIn("confirmed_after_uncertain", values)
        self.assertGreaterEqual(len(values), 7)
        for value in values:
            self.assertTrue(
                self.documented(value), f"undocumented push.result value: {value}"
            )

    def test_envelope_key_list_matches_the_boundary_matrix(self) -> None:
        def listed(text: str, marker: str) -> list[str]:
            position = text.index(marker)
            tail = text[position:]
            match = re.search(r"allocation_id[\s\S]{0,800}?worktree_path", tail)
            self.assertIsNotNone(match, f"no envelope key list after {marker!r}")
            assert match is not None
            keys = re.findall(r"[a-z_]+", match.group(0))
            self.assertEqual(keys[0], "allocation_id")
            self.assertEqual(keys[-1], "worktree_path")
            self.assertGreaterEqual(len(keys), 25, f"short key list after {marker!r}")
            return keys

        contract_keys = listed(self.doc, "Field set is mode-independent:")
        matrix_keys = listed(BOUNDARIES.read_text(encoding="utf-8"), "顶层**固定**键集合为")
        self.assertEqual(contract_keys, matrix_keys)
        self.assertEqual(len(contract_keys), len(set(contract_keys)))

    def test_conditional_keys_are_documented_in_both_places(self) -> None:
        optional = set(
            re.findall(r'^    result\["([a-z_]+)"\] = ', self.source, re.MULTILINE)
        ) - {"mode_result"}
        self.assertEqual(
            optional, {"resume", "fixture_exceptions", "reviewed_sensitive_sources", "diagnostics"}
        )
        matrix = BOUNDARIES.read_text(encoding="utf-8")
        for key in optional:
            self.assertIn(f"`{key}`", self.doc)
            self.assertIn(f"`{key}`", matrix)

    def test_readme_links_the_agent_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/agent-contract.md", readme)


if __name__ == "__main__":
    unittest.main()
