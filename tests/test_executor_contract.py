"""The executable contract does not require an executor's environment or session files."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutorContractTests(unittest.TestCase):
    def test_local_commit_and_validation_with_an_empty_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="executor-contract-") as directory:
            base = Path(directory)
            home, repo = base / "home", base / "repo"
            home.mkdir()
            repo.mkdir()
            environment = {
                "PATH": "/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }

            def git(*args: str) -> bytes:
                return subprocess.check_output(
                    ["git", "-C", str(repo), *args], env=environment, stderr=subprocess.PIPE
                )

            git("init", "-q", "--initial-branch=main")
            git("config", "user.name", "Synthetic Executor")
            git("config", "user.email", "executor@example.invalid")
            path = repo / "example.txt"
            path.write_text("first state\n")
            executable = str(ROOT / "git-finalize")
            initial = subprocess.run(
                [executable, "--summary", "--initial-commit-only", "--repo", str(repo),
                 "--message", "Synthetic initial state", "--", "example.txt"],
                env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
            self.assertTrue(json.loads(initial.stdout)["commit"]["created"])
            path.write_text("second state\n")
            before = (git("rev-parse", "HEAD"), git("ls-files", "--stage"), path.read_bytes())
            command = [executable, "--summary", "--mode", "verify-only", "--repo", str(repo),
                       "--task-key", "example-task", "--authority-key", "example-task",
                       "--", "example.txt"]
            for client in ("terminal", "executor-a", "executor-b"):
                with self.subTest(client=client):
                    result = subprocess.run(
                        command, env={**environment, "AGENT_EXECUTOR": client},
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    data = json.loads(result.stdout)
                    self.assertFalse(data["commit"]["created"])
                    self.assertFalse(data["push"]["executed"])
                    self.assertEqual(
                        (git("rev-parse", "HEAD"), git("ls-files", "--stage"), path.read_bytes()),
                        before,
                    )
            self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
