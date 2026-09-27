"""Git Finalizer publishes an authorization boundary; it does not implement authorization.

The claims under test are the ones docs/authorization-boundary.md makes to the layer that asks a
human for permission:

- the tier table is a projection of `tool_cli_contract.json`, not a second opinion;
- the CLI is non-interactive and holds no state between invocations, so it cannot be the component
  that re-asks for approval inside one task;
- the `--summary` object carries the fields a task-scoped grant has to bind to;
- the destructive and protected operations still refuse without their own explicit arguments.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tool_cli_contract.json").read_text(encoding="utf-8"))
EXECUTABLE = str(ROOT / "git-finalize")

# Fields docs/authorization-boundary.md promises an executor can bind a grant to.
SCOPE_FIELDS = (
    "repository",
    "repository_id",
    "branch",
    "upstream",
    "worktree_path",
    "mode",
    "allocation_id",
    "task_key",
    "authority_key",
    "finalizer_version",
)


def tier_table() -> list[tuple[str, str]]:
    """(interface, class) pairs read out of the documentation's tier table."""
    text = (ROOT / "docs" / "authorization-boundary.md").read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not cells[1].startswith("`"):
            continue
        operation_class = cells[1].strip("`")
        if operation_class not in CONTRACT["operation_classes"]:
            continue
        for interface in re.findall(r"`([a-z_]+)`", cells[2]):
            rows.append((interface, operation_class))
    return rows


class AuthorizationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gf-authz-")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.home = base / "home"
        self.repo = base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    # --- documentation must stay a projection of the published contract ---------------------

    def test_tier_table_matches_the_contract(self) -> None:
        declared = {command["name"]: command["operation_class"] for command in CONTRACT["commands"]}
        documented = tier_table()
        self.assertTrue(documented, "the tier table no longer parses")
        seen: dict[str, str] = {}
        for interface, operation_class in documented:
            with self.subTest(interface=interface):
                self.assertIn(interface, declared, f"{interface} is documented but not a command")
                self.assertEqual(declared[interface], operation_class)
                self.assertNotIn(interface, seen, f"{interface} is listed under two tiers")
                seen[interface] = operation_class
        self.assertEqual(
            set(declared),
            set(seen),
            "commands missing from the tier table: "
            + ", ".join(sorted(set(declared) - set(seen))),
        )

    def test_protected_operations_are_all_documented_as_ungrantable(self) -> None:
        text = (ROOT / "docs" / "authorization-boundary.md").read_text(encoding="utf-8")
        for operation in CONTRACT["protected_operations"]:
            with self.subTest(operation=operation):
                self.assertIn(
                    f"`{operation}`",
                    text,
                    "a protected operation is missing from the T3 row; an executor reading only "
                    "the tier table would treat it as grantable",
                )

    # --- the CLI cannot be the thing that asks, because it neither waits nor remembers --------

    def test_a_run_does_not_read_stdin(self) -> None:
        """An open, never-written pipe must not stall the tool."""
        self.seed_repository()
        (self.repo / "f.txt").write_text("second\n", encoding="utf-8")
        process = subprocess.Popen(
            [EXECUTABLE, "--summary", "--mode", "verify-only", "--repo", str(self.repo), "--",
             "f.txt"],  # noqa: E501
            cwd=self.repo,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - the failure we are guarding
            process.kill()
            self.fail("Git Finalizer blocked on input; it must never wait for a human")
        self.assertEqual(process.returncode, 0, stdout + stderr)

    def test_repeated_in_scope_runs_are_not_gated_by_earlier_runs(self) -> None:
        """Three commits in one task must behave like one commit that succeeded three times."""
        self.seed_repository()
        git_directory = self.repo / ".git"
        baseline = self.tree_paths(git_directory)
        for index in range(1, 4):
            with self.subTest(commit=index):
                (self.repo / "f.txt").write_text(f"state {index}\n", encoding="utf-8")
                result = self.finalize(
                    self.repo,
                    "--summary", "--mode", "commit-only", "--message", f"task step {index}", "--",
                    "f.txt",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "success")
                self.assertTrue(payload["commit"]["created"])
                self.assertFalse(payload["push"]["executed"])
                self.assertEqual(
                    self.tree_paths(git_directory) - baseline,
                    set(),
                    "a run wrote state into the repository beyond git's own object and ref stores",
                )

    def test_no_authorization_state_escapes_the_repository(self) -> None:
        self.seed_repository()
        (self.repo / "f.txt").write_text("later\n", encoding="utf-8")
        result = self.finalize(
            self.repo, "--summary", "--mode", "commit-only", "--message", "leak probe", "--",
            "f.txt",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        created = [
            path
            for path in Path(self.home).rglob("*")
            if path.is_file() or path.is_dir()
        ]
        self.assertEqual(
            sorted(str(path.relative_to(self.home)) for path in created),
            [],
            "the tool persisted something under the user's home; a grant or receipt store there "
            "would make it the component that decides whether to ask again",
        )

    # --- the summary must carry what a scoped grant binds to ---------------------------------

    def test_summary_carries_the_scope_binding_fields(self) -> None:
        self.seed_repository()
        (self.repo / "f.txt").write_text("bound\n", encoding="utf-8")
        result = self.finalize(
            self.repo,
            "--summary", "--mode", "verify-only", "--task-key", "task-one",
            "--authority-key", "authority-one", "--", "f.txt",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        for field in SCOPE_FIELDS:
            self.assertIn(field, payload, f"summary lost the {field!r} binding field")
        self.assertEqual(payload["task_key"], "task-one")
        self.assertEqual(payload["authority_key"], "authority-one")
        self.assertIsNone(payload["allocation_id"])
        self.assertEqual(payload["repository"], str(self.repo))
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["mode"], "verify-only")

    # --- protected and destructive paths keep their own gate ----------------------------------

    def test_local_retirement_refuses_without_a_controller_plan(self) -> None:
        self.seed_repository()
        result = self.finalize(
            self.repo,
            "--summary", "--retire-local-branch", "feature", "--remote", "origin",
            "--integrated-into", "main", "--expected-local-oid", "0" * 40,
            "--expected-integrated-oid", "0" * 40,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("--retirement-plan-id", payload["reason"])

    def test_first_branch_publication_refuses_a_protected_branch_name(self) -> None:
        self.seed_repository(with_remote=True)
        head = self.git("rev-parse", "HEAD").decode().strip()
        result = self.finalize(
            self.repo,
            "--summary", "--publish-existing-branch", head, "--remote", "origin",
            "--remote-branch", "main",
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["final_phase"], "preflight")
        self.assertFalse(payload["push"]["executed"])
        # `reason` is human prose and may be localised, so assert on the machine fields plus the
        # branch the refusal names, not on the wording.
        self.assertIn("main", payload["reason"])

    def test_no_force_or_tag_mutation_path_exists(self) -> None:
        """The T3 claim is structural: the entrypoint has no force-push and no tag mutation."""
        source = (ROOT / "git-finalize").read_text(encoding="utf-8")
        for line in source.splitlines():
            if not re.search(r"\bgit\b.*\bpush\b", line):
                continue
            tokens = re.findall(r"--?[A-Za-z][\w-]*", line)
            self.assertNotIn(
                "--force",
                tokens,
                f"unconditional force push path: {line!r}; only --force-with-lease=<ref>:<oid> "
                "compare-and-set is permitted",
            )
            self.assertNotIn("-f", tokens, f"short force flag: {line!r}")
        self.assertNotIn(
            "git tag", source, "the entrypoint must not create or move tags; release tagging is a "
            "separately authorised act"
        )
        self.assertIn("force_push", CONTRACT["protected_operations"])
        self.assertIn("tag_mutation", CONTRACT["protected_operations"])

    # --- helpers ------------------------------------------------------------------------------

    def git(self, *arguments: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *arguments], env=self.environment, stderr=subprocess.PIPE
        )

    def seed_repository(self, *, with_remote: bool = False) -> None:
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Authorization Probe")
        self.git("config", "user.email", "probe@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "f.txt").write_text("seed\n", encoding="utf-8")
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", "seed")
        if with_remote:
            remote = Path(self.temporary.name) / "remote.git"
            subprocess.run(
                ["git", "init", "-q", "--bare", str(remote)], env=self.environment, check=True
            )
            self.git("remote", "add", "origin", str(remote))

    def finalize(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [EXECUTABLE, "--repo", str(repository), *arguments],
            cwd=repository,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=90,
            input="",
        )

    @staticmethod
    def tree_paths(directory: Path) -> set[str]:
        """Everything under `.git` except git's own object/ref/log bookkeeping."""
        bookkeeping = re.compile(
            r"^(objects|logs|refs|hooks|branches|info|gk)(/|$)"
            r"|^(HEAD|ORIG_HEAD|MERGE_HEAD|COMMIT_EDITMSG|config|config\.gitignore|description"
            r"|index|index\.lock|packed-refs|FETCH_HEAD|CHERRY_PICK_HEAD|rebase-apply|rewritten"
            r"|gcancelet)$"
        )
        return {
            str(path.relative_to(directory))
            for path in directory.rglob("*")
            if not bookkeeping.match(str(path.relative_to(directory)))
        }


if __name__ == "__main__":
    unittest.main()
