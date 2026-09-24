#!/usr/bin/env python3
"""Validate one Worktree Controller branch-retirement plan under repo.lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

VERSION = "1.3.0"
AUTHORITY_DIRECTORIES = (
    "records",
    "bindings",
    "integration-intents",
    "release-intents",
    "release-plans",
    "release-receipts",
    "publication-receipts",
    "writer-leases",
    "transactions",
)
AUTHORITY_FILES = ("publication-lease.json",)
IDENTITY_FIELDS = (
    "schema_version",
    "repository_id",
    "allocation_id",
    "release_receipt_id",
    "branch",
    "expected_oid",
    "remote",
    "remote_ref",
    "remote_tracking_ref",
    "integrated_into",
    "integrated_ref",
    "expected_integrated_oid",
    "classification",
    "registration",
    "controller_state_digest",
)


def fail(message: str) -> None:
    print(f"ERROR: retirement plan validation blocked: {message}", file=sys.stderr)
    raise SystemExit(1)


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", os.fspath(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "Git query failed")
    return result.stdout.strip()


def canonical_path(repo: Path) -> Path:
    output = git(repo, "worktree", "list", "--porcelain")
    first = output.splitlines()[0] if output else ""
    if not first.startswith("worktree "):
        fail("cannot resolve canonical worktree")
    return Path(first.removeprefix("worktree ")).resolve(strict=True)


def authority_relevant(
    directory: str, value: dict[str, Any], *, allocation_id: str, branch: str
) -> bool:
    if value.get("allocation_id") == allocation_id:
        return True
    if directory == "integration-intents":
        return (
            allocation_id in value.get("source_allocations", [])
            or branch in value.get("source_branches", [])
        )
    if directory == "release-intents":
        return value.get("integration_allocation_id") == allocation_id
    if directory in {"release-plans", "release-receipts"}:
        return value.get("branch") == branch
    if directory == "publication-receipts":
        return any(
            isinstance(item, dict) and item.get("source_allocation_id") == allocation_id
            for item in value.get("source_results", [])
        )
    return False


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read Controller authority state: {exc}")
    if not isinstance(value, dict):
        fail("Controller authority state is not an object")
    return value


def state_digest(
    root: Path, canonical: Path, *, allocation_id: str, branch: str
) -> str:
    digest = hashlib.sha256()
    for directory_name in AUTHORITY_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            value = load_object(path)
            if not authority_relevant(
                directory_name, value, allocation_id=allocation_id, branch=branch
            ):
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    for file_name in AUTHORITY_FILES:
        path = root / file_name
        if path.is_file():
            value = load_object(path)
            if value.get("allocation_id") != allocation_id:
                continue
            digest.update(file_name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    policy = canonical / ".agents" / "worktree-policy.toml"
    digest.update(b"policy\0")
    digest.update(policy.read_bytes() if policy.is_file() else b"<default>")
    return digest.hexdigest()


def plan_id(plan: dict[str, Any]) -> str:
    identity = {field: plan.get(field) for field in IDENTITY_FIELDS}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    repo = arguments.repo.resolve(strict=True)
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    root = common / "worktree-controller" / "v1"
    plan_path = root / "branch-retirement-plans" / f"{arguments.plan_id}.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read exact Controller plan: {exc}")
    if not isinstance(plan, dict):
        fail("Controller plan is not an object")
    if plan.get("schema_version") != 1 or plan.get("plan_id") != arguments.plan_id:
        fail("Controller plan schema or identity differs")
    if plan_id(plan) != arguments.plan_id:
        fail("Controller plan content hash differs")
    expected = {
        "branch": arguments.branch,
        "remote": arguments.remote,
        "integrated_into": arguments.integrated_into,
        "expected_oid": arguments.expected_oid,
        "expected_integrated_oid": arguments.expected_integrated_oid,
        "classification": "ANCESTRY",
        "registration": "RELEASE_BACKED",
        "eligible": True,
    }
    differences = [key for key, value in expected.items() if plan.get(key) != value]
    if differences:
        fail(f"Controller plan differs for: {', '.join(differences)}")
    if plan.get("blockers") != []:
        fail("Controller plan contains blockers")
    observed_digest = state_digest(
        root,
        canonical_path(repo),
        allocation_id=str(plan["allocation_id"]),
        branch=str(plan["branch"]),
    )
    if observed_digest != plan.get("controller_state_digest"):
        fail("Controller authority state drifted after plan creation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
