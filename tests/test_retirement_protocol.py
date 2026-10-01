from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class RetirementProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="git-finalizer-retirement-protocol-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.environment = {
            **os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
        }
        self.environment_patch = mock.patch.dict(os.environ, self.environment, clear=True)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Retirement Fixture")
        self.git("config", "user.email", "retirement@example.invalid")
        (self.repo / "file.txt").write_text("fixture\n")
        self.git("add", "file.txt")
        self.git("commit", "-m", "fixture")
        self.oid = self.git("rev-parse", "HEAD")
        self.git("branch", "feat/retire")
        spec = importlib.util.spec_from_file_location("retirement_protocol", ROOT / "git-finalize-retirement-plan.py")
        assert spec is not None and spec.loader is not None
        self.protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.protocol)
        self.arguments = argparse.Namespace(
            repo=self.repo, plan_id="1" * 64, operation="local", branch="feat/retire",
            remote="origin", integrated_into="main", expected_oid=self.oid,
            expected_integrated_oid=self.oid, recover=True,
        )
        raw = Path(f"/proc/{os.getpid()}/stat").read_text()
        self.grant = {
            "schema_version": 1, "contract": "branch-retirement-execution/v1",
            "plan_id": self.arguments.plan_id, "operation": "local", "kind": "CAS_DELETE",
            "branch": self.arguments.branch, "ref": "refs/heads/feat/retire",
            "tracking_ref": "refs/remotes/origin/feat/retire", "expected_oid": self.oid,
            "integrated_ref": "refs/remotes/origin/main", "expected_integrated_oid": self.oid,
            "request_id": self.protocol.request_id(self.arguments), "authorization_id": "2" * 64,
            "token": "fixture", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "holder": {
                "pid": os.getpid(), "start_ticks": int(raw[raw.rfind(")") + 2:].split()[19]),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            },
        }
        self.calls: list[str] = []
        self.results: list[dict] = []
        self.completion_code = "RETIREMENT_EXECUTION_COMPLETE"
        self.receipt = {
            "receipt_id": "9" * 64, "plan_id": self.arguments.plan_id,
            "operation": "local", "branch": self.arguments.branch,
            "expected_oid": self.oid, "expected_integrated_oid": self.oid,
        }

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ("git", "-C", str(self.repo), *arguments), capture_output=True,
            text=True, env=self.environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def present(self) -> bool:
        result = subprocess.run(
            ("git", "-C", str(self.repo), "show-ref", "--verify", "--quiet", self.grant["ref"]),
            env=self.environment, check=False,
        )
        self.assertIn(result.returncode, (0, 1))
        return result.returncode == 0

    def dead_holder(self) -> dict:
        result = subprocess.run(
            (sys.executable, "-B", "-c", "import json,os; from pathlib import Path; "
             "pid=os.getpid(); raw=Path(f'/proc/{pid}/stat').read_text(); "
             "print(json.dumps({'pid':pid,'start_ticks':int(raw[raw.rfind(')')+2:].split()[19]),"
             "'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))"),
            capture_output=True, text=True, env=self.environment, check=True,
        )
        return json.loads(result.stdout)

    def controller(self, repo: Path, command: str, *arguments: str) -> dict:
        self.assertEqual(repo, self.repo)
        self.calls.append(command)
        if command == "branch-retirement-authorize":
            self.assertEqual(arguments[arguments.index("--request-id") + 1], self.protocol.request_id(self.arguments))
            return {"code": "RETIREMENT_AUTHORIZED", "authorization": self.grant}
        if command == "branch-retirement-complete":
            document = json.loads(Path(arguments[arguments.index("--result-file") + 1]).read_text())
            self.results.append(document)
            self.assertEqual(document["ref"], self.grant["ref"])
            self.assertEqual(document["expected_oid"], self.oid)
            return {"code": self.completion_code, "receipt": self.receipt}
        raise AssertionError(f"unexpected public command: {command}")

    def blocked(self, operation) -> str:
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics), self.assertRaises(SystemExit) as stopped:
            operation()
        self.assertEqual(stopped.exception.code, 1)
        return diagnostics.getvalue()

    def test_actual_cas_requires_controller_completion(self) -> None:
        with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller):
            result = self.protocol.execute(self.arguments)
        self.assertFalse(self.present())
        self.assertTrue(result["mutation_attempted"])
        self.assertTrue(result["controller_complete"])
        self.assertEqual(result["receipt_id"], self.receipt["receipt_id"])
        self.assertEqual(self.results[0]["outcome"], "REF_REMOVED_AT_EXPECTED_OID")
        self.assertEqual(self.calls, ["branch-retirement-authorize", "branch-retirement-complete"])

    def test_malformed_scope_holder_and_expiry_never_mutate(self) -> None:
        for field, value in (
            ("expected_oid", "3" * 40), ("schema_version", True),
            ("expires_at", "invalid"),
            ("expires_at", (datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
            ("holder", {**self.grant["holder"], "start_ticks": 0}),
        ):
            with self.subTest(field=field, value=type(value).__name__), \
                 mock.patch.dict(self.grant, {field: value}), \
                 mock.patch.object(self.protocol, "controller_call", side_effect=self.controller):
                self.blocked(lambda: self.protocol.execute(self.arguments))
                self.assertTrue(self.present())
                self.assertEqual(self.results, [])

    def test_ref_removed_but_completion_refused_is_failure_and_recoverable(self) -> None:
        self.completion_code = "RETIREMENT_EXECUTION_REFUSED"
        with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller):
            diagnostic = self.blocked(lambda: self.protocol.execute(self.arguments))
            self.assertFalse(self.present())
            self.assertIn(self.grant["authorization_id"], diagnostic)
            self.completion_code = "RETIREMENT_EXECUTION_COMPLETE"
            recovered = self.protocol.execute(self.arguments)
        self.assertFalse(recovered["mutation_attempted"])
        self.assertEqual([row["outcome"] for row in self.results],
                         ["REF_REMOVED_AT_EXPECTED_OID", "REF_ALREADY_ABSENT"])

    def test_dead_executor_rebinds_through_public_abandon(self) -> None:
        previous = {**self.grant, "holder": self.dead_holder()}

        def controller(repo: Path, command: str, *arguments: str) -> dict:
            if command == "branch-retirement-executions":
                self.calls.append(command)
                self.assertIn("abandon", arguments)
                return {"code": "RETIREMENT_EXECUTION_ABANDONED"}
            if command == "branch-retirement-authorize" and not self.calls:
                self.calls.append(command)
                return {"code": "RETIREMENT_AUTHORIZED", "authorization": previous}
            return self.controller(repo, command, *arguments)

        with mock.patch.object(self.protocol, "controller_call", side_effect=controller):
            result = self.protocol.execute(self.arguments)
        self.assertTrue(result["mutation_attempted"])
        self.assertEqual(self.calls, ["branch-retirement-authorize", "branch-retirement-executions",
                                     "branch-retirement-authorize", "branch-retirement-complete"])

    def test_dead_executor_with_absent_ref_settles_without_new_cas(self) -> None:
        self.git("update-ref", "-d", self.grant["ref"], self.oid)
        self.grant["holder"] = self.dead_holder()
        self.grant["expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller):
            result = self.protocol.execute(self.arguments)
        self.assertFalse(result["mutation_attempted"])
        self.assertNotIn("branch-retirement-executions", self.calls)
        self.assertEqual(self.results[0]["outcome"], "REF_ALREADY_ABSENT")

    def test_live_previous_executor_blocks_duplicate_cas(self) -> None:
        child = subprocess.Popen((sys.executable, "-B", "-c", "import time; time.sleep(30)"), env=self.environment)
        try:
            raw = Path(f"/proc/{child.pid}/stat").read_text()
            self.grant["holder"] = {
                **self.grant["holder"], "pid": child.pid,
                "start_ticks": int(raw[raw.rfind(")") + 2:].split()[19]),
            }
            with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller):
                diagnostic = self.blocked(lambda: self.protocol.execute(self.arguments))
            self.assertIn("another retirement executor is active", diagnostic)
            self.assertTrue(self.present())
            self.assertEqual(self.results, [])
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_completed_and_legacy_absence_do_not_authorize_new_mutation(self) -> None:
        self.git("update-ref", "-d", self.grant["ref"], self.oid)
        for code, blockers in (("RETIREMENT_OPERATION_COMPLETE", []),
                               ("RETIREMENT_BLOCKED", ["ABSENT_UNATTESTED"])):
            with self.subTest(code=code):
                calls: list[str] = []

                def controller(repo: Path, command: str, *arguments: str) -> dict:
                    calls.append(command)
                    if command == "branch-retirement-authorize":
                        return {"code": code, "blockers": blockers, "authorization": None}
                    if command == "branch-retirement-verify":
                        return {"code": "RETIREMENT_OPERATION_COMPLETE", "blockers": [],
                                "receipt": self.receipt}
                    self.assertEqual(command, "branch-retirement-record")
                    result = json.loads(Path(arguments[arguments.index("--result-file") + 1]).read_text())
                    self.assertEqual(result["kind"], "ATTEST_ABSENT")
                    return {"code": "BRANCH_RETIREMENT_RESULT_RECORDED", "receipt": self.receipt}

                with mock.patch.object(self.protocol, "controller_call", side_effect=controller):
                    result = self.protocol.execute(self.arguments)
                self.assertFalse(result["mutation_attempted"])
                self.assertEqual(result["receipt_id"], self.receipt["receipt_id"])
                self.assertEqual(len(calls), 2)

    def test_completed_receipt_must_match_the_requested_identity(self) -> None:
        self.git("update-ref", "-d", self.grant["ref"], self.oid)
        for field, value in (
            ("receipt_id", None), ("receipt_id", "not-an-identifier"),
            ("plan_id", "3" * 64), ("operation", "remote"),
            ("branch", "feat/other"), ("expected_oid", "3" * 40),
            ("expected_integrated_oid", "4" * 40),
        ):
            with self.subTest(field=field, value=value), mock.patch.dict(self.receipt, {field: value}):
                def controller(repo: Path, command: str, *arguments: str) -> dict:
                    if command == "branch-retirement-authorize":
                        return {"code": "RETIREMENT_OPERATION_COMPLETE", "blockers": [],
                                "authorization": None}
                    self.assertEqual(command, "branch-retirement-verify")
                    return {"code": "RETIREMENT_OPERATION_COMPLETE", "blockers": [],
                            "receipt": self.receipt}

                with mock.patch.object(self.protocol, "controller_call", side_effect=controller), \
                     mock.patch.object(self.protocol, "git_run", wraps=self.protocol.git_run) as git_run:
                    progress = {"mutation_attempted": False, "controller_complete": False,
                                "authorization_id": None}
                    self.blocked(lambda: self.protocol.execute(self.arguments, progress=progress))
                self.assertFalse(progress["controller_complete"])
                self.assertNotIn("receipt_id", progress)
                self.assertFalse(any(call.kwargs.get("mutation") for call in git_run.call_args_list))

    def test_completion_without_a_receipt_does_not_claim_success(self) -> None:
        self.receipt.clear()
        progress = {"mutation_attempted": False, "controller_complete": False,
                    "authorization_id": None}
        with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller):
            self.blocked(lambda: self.protocol.execute(self.arguments, progress=progress))
        self.assertFalse(self.present())
        self.assertTrue(progress["mutation_attempted"])
        self.assertFalse(progress["controller_complete"])
        self.assertNotIn("receipt_id", progress)

    def test_failed_local_cas_is_aborted_in_controller(self) -> None:
        original = self.protocol.git_run

        def git_run(repo: Path, *arguments: str, mutation: bool = False):
            if mutation:
                return subprocess.CompletedProcess(arguments, 1, "", "fixture rejection")
            return original(repo, *arguments, mutation=mutation)

        self.completion_code = "RETIREMENT_EXECUTION_ABORTED"
        with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller), \
             mock.patch.object(self.protocol, "git_run", side_effect=git_run):
            self.blocked(lambda: self.protocol.execute(self.arguments))
        self.assertTrue(self.present())
        self.assertEqual(self.results[0]["outcome"], "REF_STILL_PRESENT")

    def test_controller_failure_diagnostics_are_not_forwarded(self) -> None:
        noisy = subprocess.CompletedProcess([], 2, "fixture-hidden-stdout", "fixture-hidden-stderr")
        with mock.patch("shutil.which", return_value="fixture-controller"), \
             mock.patch("subprocess.run", return_value=noisy):
            diagnostic = self.blocked(lambda: self.protocol.controller_call(self.repo, "branch-retirement-authorize"))
        self.assertNotIn("fixture-hidden", diagnostic)
        self.assertIn("refused or failed", diagnostic)

    def test_remote_failure_keeps_authorization_without_replay(self) -> None:
        self.arguments.operation = "remote"
        self.grant.update(operation="remote", ref=self.grant["tracking_ref"],
                          request_id=self.protocol.request_id(self.arguments))
        mutations: list[tuple[str, ...]] = []

        def git_run(repo: Path, *arguments: str, mutation: bool = False):
            self.assertEqual(repo, self.repo)
            if mutation:
                mutations.append(arguments)
                return subprocess.CompletedProcess(arguments, 1, "", "fixture uncertain push")
            self.assertEqual(arguments[:2], ("ls-remote", "--heads"))
            return subprocess.CompletedProcess(arguments, 0, f"{self.oid}\trefs/heads/feat/retire\n", "")

        with mock.patch.object(self.protocol, "controller_call", side_effect=self.controller), \
             mock.patch.object(self.protocol, "git_run", side_effect=git_run):
            self.blocked(lambda: self.protocol.execute(self.arguments))
        self.assertEqual(len(mutations), 1)
        self.assertIn(f"--force-with-lease=refs/heads/feat/retire:{self.oid}", mutations[0])
        self.assertEqual(self.calls, ["branch-retirement-authorize"])
        self.assertEqual(self.results, [])


if __name__ == "__main__":
    unittest.main()
