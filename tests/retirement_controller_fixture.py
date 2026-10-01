"""Deterministic public Controller fixture for isolated retirement CLI tests.

This is the fake producer: private fixture files never cross into production
consumer code. All Git refs and capability values belong to the test repository.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

DIRECTORIES = (
    "records", "bindings", "integration-intents", "release-intents",
    "release-plans", "release-receipts", "publication-receipts",
    "writer-leases", "transactions",
)
IDENTITY = (
    "schema_version", "repository_id", "allocation_id", "release_receipt_id",
    "branch", "expected_oid", "remote", "remote_ref", "remote_tracking_ref",
    "integrated_into", "integrated_ref", "expected_integrated_oid",
    "classification", "registration", "controller_state_digest",
)


def emit(decision: dict, status: int = 0) -> None:
    print(json.dumps({"decision": decision}, separators=(",", ":")))
    raise SystemExit(status)


def query(repo: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments), capture_output=True, text=True, check=False,
    )
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise ValueError("fixture query refused")
    return result.stdout.strip()


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n")


def load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("fixture document is not an object")
    return value


def digest(root: Path, repo: Path) -> str:
    value = hashlib.sha256()
    for name in DIRECTORIES:
        directory = root / name
        if directory.is_dir():
            for path in sorted(directory.rglob("*.json")):
                value.update(path.relative_to(root).as_posix().encode())
                value.update(b"\0")
                value.update(path.read_bytes())
                value.update(b"\0")
    policy = repo / ".agents/worktree-policy.toml"
    value.update(b"policy\0")
    value.update(policy.read_bytes() if policy.is_file() else b"<default>")
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    for name in ("repo", "plan-id", "operation", "request-id", "holder", "client-id", "run-id",
                 "authorization-id", "token", "result-file", "action", "reason"):
        parser.add_argument("--" + name)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    if arguments.command == "capabilities":
        emit({
            "branch_retirement_version": 1, "branch_retirement_verify_version": 1,
            "branch_retirement_execution_version": 1,
        })
    repo = Path(arguments.repo)
    root = Path(query(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "worktree-controller/v1"
    if not (root / "repo.lock").is_file() or (root / "repo.lock").is_symlink():
        raise ValueError("fixture authority lock missing")
    grants_root = root / "branch-retirement-authorizations"
    grants = [load(path) for path in sorted(grants_root.glob("*.json"))] if grants_root.exists() else []
    if arguments.command == "branch-retirement-executions" and arguments.action == "list":
        emit({"code": "RETIREMENT_EXECUTIONS_LISTED", "authorizations": [
            {key: row[key] for key in ("authorization_id", "plan_id", "operation")}
            for row in grants
        ]})
    if arguments.authorization_id:
        grant_path = root / "branch-retirement-authorizations" / (arguments.authorization_id + ".json")
        grant = load(grant_path)
        arguments.plan_id = grant["plan_id"]
        arguments.operation = grant["operation"]
    plan = load(root / "branch-retirement-plans" / (arguments.plan_id + ".json"))
    identity = {field: plan.get(field) for field in IDENTITY}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if type(plan.get("schema_version")) is not int or plan.get("schema_version") != 1:
        raise ValueError("fixture plan schema differs")
    if hashlib.sha256(encoded.encode()).hexdigest() != arguments.plan_id:
        raise ValueError("fixture plan content hash differs")
    current = dict(plan)
    current["blockers"] = [] if digest(root, repo) == plan["controller_state_digest"] else ["PLAN_STATE_DRIFT"]
    local = query(repo, "rev-parse", "--verify", "--quiet", plan["remote_ref"])
    tracking = query(repo, "rev-parse", "--verify", "--quiet", plan["remote_tracking_ref"])
    current["local_ref_state"] = "ABSENT" if local is None else ("PRESENT_EXPECTED" if local == plan["expected_oid"] else "PRESENT_OTHER")
    current["remote_tracking_state"] = "ABSENT" if tracking is None else ("PRESENT_EXPECTED" if tracking == plan["expected_oid"] else "PRESENT_OTHER")
    operation = arguments.operation or "local"
    ref = plan["remote_ref"] if operation == "local" else plan["remote_tracking_ref"]
    state = current["local_ref_state"] if operation == "local" else current["remote_tracking_state"]
    receipt_path = root / "branch-retirement-receipts" / f"{arguments.plan_id}-{operation}.json"
    receipt = load(receipt_path) if receipt_path.is_file() else None
    active = [row for row in grants if row["plan_id"] == arguments.plan_id and row["operation"] == operation]
    blockers = list(current["blockers"])
    if active:
        blockers.append("RETIREMENT_AUTHORIZATION_ACTIVE")
    code = ("RETIREMENT_OPERATION_COMPLETE" if receipt is not None else
            "RETIREMENT_BLOCKED" if blockers or state != "PRESENT_EXPECTED" else "RETIREMENT_EXECUTABLE")
    if receipt is None and state == "ABSENT":
        blockers.append("ABSENT_UNATTESTED")
    minimum = {
        "plan_id": arguments.plan_id, "operation": operation, "branch": plan["branch"],
        "expected_oid": plan["expected_oid"], "expected_integrated_oid": plan["expected_integrated_oid"],
        "integrated_ref": plan["integrated_ref"], "ref_state": state,
    }
    if arguments.command == "branch-retirement-status":
        emit({"code": "BRANCH_RETIREMENT_READY", "plan_id": arguments.plan_id, "current": current})
    if arguments.command == "branch-retirement-verify":
        emit({"code": code, "blockers": sorted(blockers), "minimum_revalidation": minimum, "receipt": receipt})
    if arguments.command == "branch-retirement-executions":
        if arguments.action == "inspect":
            emit({"code": "RETIREMENT_EXECUTION_INSPECTED", "authorization": {
                key: value for key, value in grant.items() if key != "token"
            }})
        if arguments.action == "abandon" and arguments.token == grant["token"]:
            grant_path.unlink()
            emit({"code": "RETIREMENT_EXECUTION_ABANDONED"})
        raise ValueError("fixture authorization action refused")
    if arguments.command == "branch-retirement-authorize":
        replay = next((row for row in active if row["request_id"] == arguments.request_id), None)
        if replay is not None:
            emit({"code": "RETIREMENT_AUTHORIZED", "authorization": replay})
        if code != "RETIREMENT_EXECUTABLE":
            emit({"code": code, "blockers": sorted(blockers), "authorization": None})
        pid = os.getppid()
        raw = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(raw[raw.rfind(")") + 2:].split()[19])
        issued = datetime.now(UTC)
        grant = {
            "schema_version": 1, "contract": "branch-retirement-execution/v1",
            "request_id": arguments.request_id, "plan_id": arguments.plan_id,
            "operation": operation, "kind": "CAS_DELETE", "branch": plan["branch"], "ref": ref,
            "expected_oid": plan["expected_oid"], "expected_integrated_oid": plan["expected_integrated_oid"],
            "integrated_ref": plan["integrated_ref"], "tracking_ref": plan["remote_tracking_ref"],
            "holder": {"pid": pid, "start_ticks": ticks, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()},
            "issued_at": issued.isoformat(), "expires_at": (issued + timedelta(seconds=900)).isoformat(),
            "token": "fixture",
        }
        grant["authorization_id"] = hashlib.sha256(json.dumps(grant, sort_keys=True).encode()).hexdigest()
        write(grants_root / (grant["authorization_id"] + ".json"), grant)
        emit({"code": "RETIREMENT_AUTHORIZED", "authorization": grant})
    if arguments.command in {"branch-retirement-complete", "branch-retirement-record"}:
        result = load(Path(arguments.result_file))
        if result.get("plan_id") != arguments.plan_id or result.get("operation") != operation:
            raise ValueError("fixture result identity differs")
        absent = query(repo, "rev-parse", "--verify", "--quiet", ref) is None
        if arguments.command == "branch-retirement-complete":
            if arguments.token != grant["token"]:
                raise ValueError("fixture capability differs")
            if os.environ.get("GF_RETIREMENT_FIXTURE_REFUSE_COMPLETE") == "1":
                emit({"code": "RETIREMENT_EXECUTION_REFUSED"})
            if result["outcome"] == "REF_STILL_PRESENT" and not absent:
                grant_path.unlink()
                emit({"code": "RETIREMENT_EXECUTION_ABORTED"})
            if not absent or current["blockers"]:
                emit({"code": "RETIREMENT_EXECUTION_REFUSED"})
            grant_path.unlink()
            provenance = "AUTHORIZED_EXECUTION"
            response_code = "RETIREMENT_EXECUTION_COMPLETE"
        else:
            if active or not absent or current["blockers"]:
                raise ValueError("fixture absence observation refused")
            provenance = "CONTROLLER_OBSERVED"
            response_code = "BRANCH_RETIREMENT_RESULT_RECORDED"
        receipt = {
            "schema_version": 1, "plan_id": arguments.plan_id,
            "operation": operation, "provenance": provenance,
            "receipt_id": hashlib.sha256(f"fixture-receipt/{arguments.plan_id}/{operation}".encode()).hexdigest(),
            "branch": plan["branch"], "expected_oid": plan["expected_oid"],
            "expected_integrated_oid": plan["expected_integrated_oid"],
        }
        write(receipt_path, receipt)
        emit({"code": response_code, "receipt": receipt})
    raise ValueError("unknown fixture operation")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError):
        emit({"code": "SYNTHETIC_CONTROLLER_REFUSED"}, 2)
