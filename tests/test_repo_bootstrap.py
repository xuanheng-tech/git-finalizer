from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repo_bootstrap", ROOT / "codex-git-finalize-repo-bootstrap.py"
)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GiteaState:
    def __init__(self) -> None:
        self.username = "alice"
        self.organizations = {"team"}
        self.repositories: dict[str, dict[str, object]] = {}
        self.posts: list[dict[str, object]] = []
        self.deny = False
        self.invalid_json = False
        self.delay = 0.0
        self.next_id = 100

    def repository(self, owner: str, name: str, *, private: bool, empty: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.next_id,
            "name": name,
            "owner": {"login": owner},
            "private": private,
            "empty": empty,
        }
        self.next_id += 1
        self.repositories[f"{owner}/{name}"] = value
        return value


class FakeGitea:
    def __init__(self) -> None:
        self.state = GiteaState()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_arguments: object) -> None:
                return

            def _send(self, status: int, value: object) -> None:
                if state.delay:
                    time.sleep(state.delay)
                raw = b"{invalid" if state.invalid_json else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except BrokenPipeError:
                    pass

            def do_GET(self) -> None:
                if state.deny:
                    self._send(403, {"message": "denied"})
                    return
                path = urllib.parse.urlsplit(self.path).path.removeprefix("/api/v1")
                if path == "/user":
                    self._send(200, {"login": state.username})
                    return
                if path == "/user/orgs":
                    self._send(200, [{"username": name} for name in sorted(state.organizations)])
                    return
                if path.startswith("/orgs/"):
                    owner = urllib.parse.unquote(path.removeprefix("/orgs/"))
                    self._send(200 if owner in state.organizations else 404, {"username": owner})
                    return
                if path.startswith("/repos/"):
                    key = urllib.parse.unquote(path.removeprefix("/repos/"))
                    repository = state.repositories.get(key)
                    self._send(200 if repository else 404, repository or {"message": "not found"})
                    return
                self._send(404, {"message": "not found"})

            def do_POST(self) -> None:
                if state.deny:
                    self._send(403, {"message": "denied"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                state.posts.append(payload)
                path = urllib.parse.urlsplit(self.path).path.removeprefix("/api/v1")
                if path == "/user/repos":
                    owner = state.username
                elif path.startswith("/orgs/") and path.endswith("/repos"):
                    owner = urllib.parse.unquote(path.removeprefix("/orgs/").removesuffix("/repos"))
                else:
                    self._send(404, {"message": "not found"})
                    return
                created = state.repository(
                    owner,
                    payload["name"],
                    private=payload["private"],
                    empty=payload.get("auto_init") is False,
                )
                self._send(201, created)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeGitea:
        self.thread.start()
        return self

    def __exit__(self, *_arguments: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RepositoryBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="git-finalizer-bootstrap-")
        self.repo = Path(self.temporary.name) / "repo"
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True
        )
        git(self.repo, "config", "user.name", "Bootstrap Test")
        git(self.repo, "config", "user.email", "bootstrap@example.invalid")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "initial")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def arguments(
        self,
        server: FakeGitea,
        *,
        ensure: bool = False,
        owner: str = "alice",
        visibility: str = "private",
        add_origin: bool = False,
        timeout: float = 2.0,
    ) -> object:
        values = [
            "--repo-ensure" if ensure else "--repo-plan",
            "--repo",
            str(self.repo),
            "--gitea-url",
            server.url,
            "--gitea-profile",
            "disposable-test",
            "--owner",
            owner,
            "--repo-name",
            "example",
            "--visibility",
            visibility,
            "--timeout",
            str(timeout),
            "--summary",
            "--repository-id",
            "controller-repository",
            "--allocation-id",
            "allocation",
            "--task-key",
            "task",
            "--authority-key",
            "authority",
            "--worktree-path",
            str(self.repo),
            "--role",
            "control-plane",
        ]
        if add_origin:
            values.append("--add-origin")
        return bootstrap.build_parser().parse_args(values)

    def execute(self, arguments: object, password: str = "fake-token") -> tuple[int, dict[str, object]]:
        credential = bootstrap.Credential(username="alice", password=password)
        with mock.patch.object(bootstrap, "resolve_credential", return_value=credential):
            return bootstrap.execute(arguments)

    def test_repo_plan_nonexistent_and_existing_match(self) -> None:
        with FakeGitea() as server:
            code, missing = self.execute(self.arguments(server))
            self.assertEqual(code, 0)
            self.assertEqual(missing["decision"], "CREATE_ALLOWED")
            server.state.repository("alice", "example", private=True)
            code, existing = self.execute(self.arguments(server))
            self.assertEqual(code, 0)
            self.assertEqual(existing["decision"], "ALREADY_EXISTS_MATCH")

    def test_existing_visibility_mismatch_is_blocked(self) -> None:
        with FakeGitea() as server:
            server.state.repository("alice", "example", private=False)
            code, receipt = self.execute(self.arguments(server, visibility="private"))
            self.assertEqual(code, 2)
            self.assertEqual(receipt["decision"], "BLOCK_REMOTE_MISMATCH")

    def test_permission_denied_and_invalid_owner(self) -> None:
        with FakeGitea() as server:
            server.state.deny = True
            code, denied = self.execute(self.arguments(server))
            self.assertEqual(code, 2)
            self.assertEqual(denied["decision"], "BLOCK_PERMISSION")
        with FakeGitea() as server:
            code, invalid = self.execute(self.arguments(server, owner="missing-owner"))
            self.assertEqual(code, 2)
            self.assertEqual(invalid["decision"], "BLOCK_INVALID_OWNER")

    def test_origin_missing_correct_and_conflicting(self) -> None:
        with FakeGitea() as server:
            server.state.repository("alice", "example", private=True)
            code, missing = self.execute(self.arguments(server))
            self.assertEqual(code, 0)
            self.assertEqual(missing["remote"]["state"], "missing")
            expected = f"{server.url}/alice/example.git"
            git(self.repo, "remote", "add", "origin", expected)
            code, correct = self.execute(self.arguments(server))
            self.assertEqual(code, 0)
            self.assertEqual(correct["remote"]["state"], "correct")
            git(self.repo, "remote", "set-url", "origin", f"{server.url}/alice/other.git")
            code, conflict = self.execute(self.arguments(server))
            self.assertEqual(code, 2)
            self.assertEqual(conflict["decision"], "BLOCK_REMOTE_MISMATCH")

    def test_create_is_idempotent_empty_and_does_not_publish(self) -> None:
        with FakeGitea() as server:
            arguments = self.arguments(server, ensure=True)
            first_code, first = self.execute(arguments)
            second_code, second = self.execute(arguments)
            self.assertEqual((first_code, second_code), (0, 0))
            self.assertEqual(len(server.state.posts), 1)
            self.assertFalse(server.state.posts[0]["auto_init"])
            self.assertEqual(first["bootstrap"]["outcome"], "created_empty_verified")
            self.assertEqual(second["bootstrap"]["outcome"], "already_exists_verified")
            self.assertEqual(first["publication"], {"executed": False, "outcome": "not_run"})
            self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "1")

    def test_add_origin_only_when_missing(self) -> None:
        with FakeGitea() as server:
            code, receipt = self.execute(self.arguments(server, ensure=True, add_origin=True))
            self.assertEqual(code, 0)
            self.assertEqual(receipt["remote"]["outcome"], "added_verified")
            self.assertEqual(
                git(self.repo, "remote", "get-url", "origin"),
                f"{server.url}/alice/example.git",
            )

    def test_organization_creation_uses_explicit_owner(self) -> None:
        with FakeGitea() as server:
            code, receipt = self.execute(self.arguments(server, ensure=True, owner="team"))
            self.assertEqual(code, 0)
            self.assertEqual(receipt["bootstrap"]["owner_kind"], "organization")
            self.assertIn("team/example", server.state.repositories)

    def test_invalid_json_and_timeout_are_unknown(self) -> None:
        with FakeGitea() as server:
            server.state.invalid_json = True
            code, invalid = self.execute(self.arguments(server))
            self.assertEqual(code, 3)
            self.assertEqual(invalid["decision"], "UNKNOWN")
        with FakeGitea() as server:
            server.state.delay = 0.2
            code, timed_out = self.execute(self.arguments(server, timeout=0.02))
            self.assertEqual(code, 3)
            self.assertEqual(timed_out["decision"], "UNKNOWN")

    def test_receipt_schema_linkage_and_token_redaction(self) -> None:
        marker = "TOKEN-MUST-NOT-LEAK"
        with FakeGitea() as server:
            code, receipt = self.execute(self.arguments(server), password=marker)
            self.assertEqual(code, 0)
            self.assertEqual(receipt["summary_schema_version"], 1)
            self.assertEqual(receipt["finalizer_version"], "0.8.0")
            self.assertEqual(receipt["repository_id"], "controller-repository")
            self.assertEqual(receipt["allocation_id"], "allocation")
            self.assertEqual(receipt["task_key"], "task")
            self.assertEqual(receipt["authority_key"], "authority")
            self.assertEqual(receipt["role"], "control-plane")
            self.assertNotIn(marker, json.dumps(receipt))

    def test_git_credential_resolution_is_not_exposed(self) -> None:
        with FakeGitea() as server:
            arguments = self.arguments(server)
            target = bootstrap._target(arguments)
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="username=alice\npassword=secret\n", stderr=""
            )
            with mock.patch.object(bootstrap, "_git", return_value=completed):
                credential = bootstrap.resolve_credential(target)
            self.assertEqual(credential.username, "alice")
            self.assertEqual(credential.password, "secret")

    def test_public_entrypoint_dispatches_without_exposing_credential(self) -> None:
        marker = "ENTRYPOINT-FAKE-TOKEN"
        helper = f"!f() {{ printf 'username=alice\\npassword={marker}\\n'; }}; f"
        git(self.repo, "config", "credential.helper", helper)
        with FakeGitea() as server:
            result = subprocess.run(
                [
                    str(ROOT / "codex-git-finalize"),
                    "--repo-plan",
                    "--repo",
                    str(self.repo),
                    "--gitea-url",
                    server.url,
                    "--owner",
                    "alice",
                    "--repo-name",
                    "example",
                    "--visibility",
                    "private",
                    "--summary",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "CREATE_ALLOWED")
        self.assertNotIn(marker, result.stdout)
        self.assertNotIn(marker, result.stderr)


if __name__ == "__main__":
    unittest.main()
