#!/usr/bin/env python3
"""Consume the public Controller retirement authorization/completion protocol.

Controller decisions and state digests belong to the producer. No Controller
metadata files or digest recipes are read here. Actual CAS operations receive a
single-use grant; interrupted runs recover the same request through public APIs.
Legacy already-absent observations use the producer's typed compatibility record.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

# GF-INTEGRATION-ADAPTER: worktree-controller-retirement (single site)
VERSION = "1.8.2"
BRANCH_RETIREMENT_CAPABILITY = "branch_retirement_version"
PLAN_ID_RE = re.compile(r"[0-9a-f]{64}")
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def fail(message: str) -> None:
    print(f"ERROR: retirement plan validation blocked: {message}", file=sys.stderr)
    raise SystemExit(1)


def object_value(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        fail(f"unreadable Controller {label}")
    return value


def controller_call(repo: Path, command: str, *arguments: str) -> dict:
    executable = shutil.which("worktree-controller")
    if executable is None:
        fail("Worktree Controller is required for plan-bound retirement")
    try:
        result = subprocess.run(
            (executable, command, "--repo", str(repo), "--json", *arguments),
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        fail(f"Controller {command} failed or timed out")
    if result.returncode != 0:
        # Producer/hook diagnostics may contain capability tokens or URL credentials.
        fail(f"Controller {command} refused or failed")
    try:
        document = object_value(json.loads(result.stdout), "response")
        return object_value(document.get("decision"), "decision")
    except (TypeError, ValueError, UnicodeError):
        fail(f"unreadable Controller {command} response")


def attest_controller_capability(repo: Path, plan_schema: int) -> None:
    decision = controller_call(repo, "capabilities")
    for name, expected in (
        (BRANCH_RETIREMENT_CAPABILITY, plan_schema),
        ("branch_retirement_verify_version", 1),
        ("branch_retirement_execution_version", 1),
    ):
        version = decision.get(name)
        if type(version) is not int or version != expected:
            fail("Controller does not advertise the required public retirement protocols")


def integration_core(value: str, remote: str) -> str:
    for prefix in (f"refs/remotes/{remote}/", "refs/heads/", f"{remote}/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def request_id(arguments: argparse.Namespace) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"git-finalizer-retirement/{arguments.plan_id}/{arguments.operation}",
    ))


def identity_matches(arguments: argparse.Namespace, document: dict, *, grant: bool = False) -> None:
    expected = {
        "schema_version": 1,
        "plan_id": arguments.plan_id,
        "branch": arguments.branch,
        "expected_oid": arguments.expected_oid,
    }
    if grant:
        expected.update(
            schema_version=1, contract="branch-retirement-execution/v1",
            operation=arguments.operation, kind="CAS_DELETE",
            request_id=request_id(arguments),
            ref=(f"refs/heads/{arguments.branch}" if arguments.operation == "local"
                 else f"refs/remotes/{arguments.remote}/{arguments.branch}"),
            tracking_ref=f"refs/remotes/{arguments.remote}/{arguments.branch}",
        )
    else:
        expected["remote"] = arguments.remote
    if type(document.get("schema_version")) is not int:
        fail("Controller retirement schema differs")
    if arguments.expected_integrated_oid:
        expected["expected_integrated_oid"] = arguments.expected_integrated_oid
    if any(document.get(key) != value for key, value in expected.items()):
        fail("Controller retirement identity differs from the requested operation")
    integrated_ref = document.get("integrated_ref")
    if not isinstance(integrated_ref, str) or integrated_ref != (
        f"refs/remotes/{arguments.remote}/"
        f"{integration_core(arguments.integrated_into, arguments.remote)}"
    ):
        fail("Controller retirement integration ref differs")
    if OID_RE.fullmatch(str(document.get("expected_integrated_oid", ""))) is None:
        fail("Controller retirement integration OID is unreadable")


def actor_is_live(grant: dict) -> bool:
    holder = object_value(grant.get("holder"), "authorization holder")
    pid, ticks = holder.get("pid"), holder.get("start_ticks")
    if type(pid) is not int or pid < 1 or type(ticks) is not int:
        fail("unreadable authorization holder identity")
    try:
        if holder.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            return False
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    except OSError:
        fail("authorization holder liveness is unavailable")
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return int(fields[19]) == ticks and fields[0] != "Z"
    except (ValueError, IndexError):
        fail("authorization holder liveness is unreadable")


def own_pending(arguments: argparse.Namespace) -> dict | None:
    decision = controller_call(arguments.repo, "branch-retirement-executions", "--action", "list")
    rows = decision.get("authorizations")
    if not isinstance(rows, list):
        fail("unreadable outstanding retirement authorizations")
    for row in rows:
        row = object_value(row, "authorization listing")
        if row.get("plan_id") != arguments.plan_id or row.get("operation") != arguments.operation:
            continue
        identifier = row.get("authorization_id")
        if not isinstance(identifier, str) or PLAN_ID_RE.fullmatch(identifier) is None:
            fail("unreadable retirement authorization identifier")
        inspected = controller_call(
            arguments.repo, "branch-retirement-executions", "--action", "inspect",
            "--authorization-id", identifier,
        )
        grant = object_value(inspected.get("authorization"), "authorization")
        if grant.get("authorization_id") != identifier:
            fail("Controller retirement authorization identifier differs")
        if grant.get("request_id") != request_id(arguments):
            fail("another retirement executor owns this operation")
        identity_matches(arguments, grant, grant=True)
        if actor_is_live(grant):
            fail(f"retirement executor is still active; authorization_id={identifier}")
        return grant
    return None


def preflight(arguments: argparse.Namespace) -> None:
    status = controller_call(
        arguments.repo, "branch-retirement-status", "--plan-id", arguments.plan_id,
    )
    current = object_value(status.get("current"), "retirement status")
    identity_matches(arguments, current)
    verdict = controller_call(
        arguments.repo, "branch-retirement-verify", "--plan-id", arguments.plan_id,
        "--operation", arguments.operation,
    )
    blockers = verdict.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        fail("unreadable retirement verdict blockers")
    minimum = object_value(verdict.get("minimum_revalidation"), "retirement observation")
    if any(minimum.get(key) != value for key, value in {
        "plan_id": arguments.plan_id, "operation": arguments.operation, "branch": arguments.branch,
        "expected_oid": arguments.expected_oid, "integrated_ref": current["integrated_ref"],
        "expected_integrated_oid": current["expected_integrated_oid"],
    }.items()):
        fail("Controller retirement verdict identity differs")
    if verdict.get("code") == "RETIREMENT_OPERATION_COMPLETE":
        if minimum.get("ref_state") != "ABSENT":
            fail("Controller already records a consumed receipt while its ref remains")
    if verdict.get("code") in {"RETIREMENT_EXECUTABLE", "RETIREMENT_OPERATION_COMPLETE"} and not blockers:
        return
    # A legacy plan-only consumer may already have removed the ref. The producer
    # must observe and record that absence; this never grants a fresh CAS.
    if verdict.get("code") == "RETIREMENT_BLOCKED" and blockers == ["ABSENT_UNATTESTED"]:
        return
    if (arguments.recover and "RETIREMENT_AUTHORIZATION_ACTIVE" in blockers
            and set(blockers) <= {"RETIREMENT_AUTHORIZATION_ACTIVE", "ABSENT_UNATTESTED"}
            and own_pending(arguments) is not None):
        return
    fail("Controller retirement verdict blocks this operation")


def git_run(repo: Path, *arguments: str, mutation: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ("git", "--no-optional-locks", "-C", str(repo), *arguments),
            capture_output=True, text=True, check=False,
            timeout=60 if mutation else 30,
            env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("Git retirement outcome is uncertain; inspect the outstanding authorization")


def observed_oid(arguments: argparse.Namespace) -> str | None:
    ref = f"refs/heads/{arguments.branch}"
    if arguments.operation == "local":
        result = git_run(arguments.repo, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
        if result.returncode == 1:
            return None
        value = result.stdout.strip()
    else:
        result = git_run(arguments.repo, "ls-remote", "--heads", arguments.remote, ref)
        if result.returncode == 0 and not result.stdout.strip():
            return None
        lines = result.stdout.strip().splitlines()
        if len(lines) != 1 or lines[0].split("\t")[-1] != ref:
            fail("live retirement remote ref is unreadable")
        value = lines[0].split("\t")[0]
    if result.returncode != 0 or OID_RE.fullmatch(value) is None:
        fail("Git retirement ref query failed")
    if value != arguments.expected_oid:
        fail("live retirement ref changed from its expected OID")
    return value


def reported_result(arguments: argparse.Namespace, grant: dict | None, outcome: str) -> dict:
    return {
        "schema_version": 1, "contract": "retirement-execution-result/v1",
        "plan_id": arguments.plan_id, "operation": arguments.operation,
        "kind": "CAS_DELETE" if grant is not None else "ATTEST_ABSENT",
        "outcome": outcome,
        "ref": (f"refs/heads/{arguments.branch}" if arguments.operation == "local"
                else f"refs/remotes/{arguments.remote}/{arguments.branch}"),
        "expected_oid": arguments.expected_oid,
        "integrated_ref": (f"refs/remotes/{arguments.remote}/"
                           f"{integration_core(arguments.integrated_into, arguments.remote)}"),
        "expected_integrated_oid": (
            grant["expected_integrated_oid"] if grant is not None
            else arguments.expected_integrated_oid
        ),
        "executor": {"name": "git-finalizer", "version": VERSION},
    }


def settle(arguments: argparse.Namespace, grant: dict | None, outcome: str) -> None:
    result = reported_result(arguments, grant, outcome)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="git-finalizer-retirement-result-") as handle:
        json.dump(result, handle)
        handle.flush()
        if grant is None:
            decision = controller_call(
                arguments.repo, "branch-retirement-record", "--plan-id", arguments.plan_id,
                "--operation", arguments.operation, "--result-file", handle.name,
            )
            if decision.get("code") != "BRANCH_RETIREMENT_RESULT_RECORDED":
                fail("Controller refused the already-absent retirement observation")
        else:
            identifier = grant["authorization_id"]
            decision = controller_call(
                arguments.repo, "branch-retirement-complete", "--authorization-id", identifier,
                "--token", grant["token"], "--result-file", handle.name,
            )
            expected = ("RETIREMENT_EXECUTION_COMPLETE" if outcome != "REF_STILL_PRESENT"
                        else "RETIREMENT_EXECUTION_ABORTED")
            if decision.get("code") != expected:
                fail(f"Controller retirement completion is unverified; authorization_id={identifier}")


def execute(arguments: argparse.Namespace, *, recovered: bool = False, progress: dict | None = None) -> dict:
    if progress is None:
        progress = {"mutation_attempted": False, "controller_complete": False, "authorization_id": None}
    before = observed_oid(arguments)
    decision = controller_call(
        arguments.repo, "branch-retirement-authorize", "--plan-id", arguments.plan_id,
        "--operation", arguments.operation, "--request-id", request_id(arguments),
        "--holder", f"ancestor:{Path(sys.executable).resolve(strict=True).name}",
        "--client-id", "git-finalizer", "--run-id", request_id(arguments),
    )
    grant = decision.get("authorization")
    if (decision.get("code") == "RETIREMENT_OPERATION_COMPLETE" and grant is None
            and before is None and decision.get("blockers") == []):
        progress["controller_complete"] = True
        return progress
    if (decision.get("code") == "RETIREMENT_BLOCKED" and grant is None and before is None
            and decision.get("blockers") == ["ABSENT_UNATTESTED"]):
        settle(arguments, None, "REF_ALREADY_ABSENT")
        progress["controller_complete"] = True
        return progress
    if decision.get("code") != "RETIREMENT_AUTHORIZED":
        fail("Controller did not authorize the retirement CAS")
    grant = object_value(grant, "authorization")
    identity_matches(arguments, grant, grant=True)
    identifier, token = grant.get("authorization_id"), grant.get("token")
    if (not isinstance(identifier, str) or PLAN_ID_RE.fullmatch(identifier) is None
            or not isinstance(token, str) or not token):
        fail("Controller authorization identity or capability is unreadable")
    progress["authorization_id"] = identifier
    holder = object_value(grant.get("holder"), "authorization holder")
    if holder.get("pid") == os.getpid() and not actor_is_live(grant):
        fail("Controller authorization does not bind this executor identity")
    if holder.get("pid") != os.getpid():
        if actor_is_live(grant):
            fail(f"another retirement executor is active; authorization_id={identifier}")
        if before is not None:
            # Rebind a dead previous executor through the producer, never by editing
            # persisted grants. Ref absence instead settles the original attempt.
            if recovered:
                fail(f"Controller did not rebind the recovered executor; authorization_id={identifier}")
            abandoned = controller_call(
                arguments.repo, "branch-retirement-executions", "--action", "abandon",
                "--authorization-id", identifier, "--token", token,
                "--reason", "same-request recovery: previous executor ended, expected ref remains",
            )
            if abandoned.get("code") != "RETIREMENT_EXECUTION_ABANDONED":
                fail(f"Controller refused executor recovery; authorization_id={identifier}")
            return execute(arguments, recovered=True, progress=progress)
    try:
        expiry = datetime.fromisoformat(grant["expires_at"])
        if expiry.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        fail("Controller authorization expiry is unreadable")
    if before is not None and expiry <= datetime.now(UTC):
        fail(f"retirement authorization expired; authorization_id={identifier}")

    attempted = before is not None
    if attempted:
        progress["mutation_attempted"] = True
        ref = f"refs/heads/{arguments.branch}"
        if arguments.operation == "local":
            mutation = git_run(arguments.repo, "update-ref", "-d", ref, arguments.expected_oid, mutation=True)
        else:
            mutation = git_run(
                arguments.repo, "push", "--no-follow-tags",
                f"--force-with-lease={ref}:{arguments.expected_oid}",
                arguments.remote, f":{ref}", mutation=True,
            )
        after = observed_oid(arguments)
        if after is not None:
            if arguments.operation == "local":
                settle(arguments, grant, "REF_STILL_PRESENT")
            fail(f"retirement CAS did not remove the ref; authorization_id={identifier}")
        outcome = "REF_REMOVED_AT_EXPECTED_OID" if mutation.returncode == 0 else "REF_ALREADY_ABSENT"
    else:
        outcome = "REF_ALREADY_ABSENT"
    if arguments.operation == "remote":
        if git_run(arguments.repo, "fetch", "--prune", "--no-tags", arguments.remote).returncode != 0:
            fail(f"retirement tracking observation failed; authorization_id={identifier}")
    settle(arguments, grant, outcome)
    progress["controller_complete"] = True
    return progress


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--operation", choices=("local", "remote"), required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--integrated-into", required=True)
    parser.add_argument("--expected-oid", required=True)
    parser.add_argument("--expected-integrated-oid", required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if PLAN_ID_RE.fullmatch(arguments.plan_id) is None or OID_RE.fullmatch(arguments.expected_oid) is None:
        fail("plan or expected OID identifier is unreadable")
    arguments.repo = arguments.repo.resolve(strict=True)
    attest_controller_capability(arguments.repo, 1)
    preflight(arguments)
    if arguments.execute:
        progress = {"mutation_attempted": False, "controller_complete": False, "authorization_id": None}
        try:
            try:
                execute(arguments, progress=progress)
            except (OSError, ValueError, TypeError, KeyError):
                fail("retirement protocol input or result is unreadable")
        except SystemExit:
            print(json.dumps(progress, separators=(",", ":")), flush=True)
            raise
        print(json.dumps(progress, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError):
        fail("retirement protocol input or result is unreadable")
