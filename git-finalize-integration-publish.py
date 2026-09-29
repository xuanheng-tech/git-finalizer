#!/usr/bin/env python3
"""Publish one Controller-leased integration candidate without touching canonical checkout state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, NoReturn, Sequence
import uuid


VERSION = "1.6.1"
OID = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class PublishError(Exception):
    """A publication invariant failed before verified completion."""


def run_git(
    repo: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "LC_ALL": "C",
        }
    )
    result = subprocess.run(
        (
            "git",
            "--no-optional-locks",
            "-C",
            os.fspath(repo),
            *arguments,
        ),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise PublishError(f"Git command failed (exit {result.returncode}); inspect Git diagnostics locally")
    return result


def git_text(repo: Path, *arguments: str) -> str:
    return run_git(repo, *arguments).stdout.strip()


def read_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PublishError(f"{label} is missing or not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublishError(f"{label} must be a JSON object")
    return value


def require_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PublishError(f"{label} must be a UUID")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise PublishError(f"{label} must be a UUID") from exc
    return value


def require_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or OID.fullmatch(value) is None:
        raise PublishError(f"{label} must be a full SHA-1 commit OID")
    return value


def require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise PublishError(f"{label} must be a SHA-256 digest")
    return value


def require_positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PublishError(f"{label} must be a positive integer")
    return value


def require_text(value: object, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\n", "\r", "\t"))
    ):
        raise PublishError(f"{label} must be a bounded single-line value")
    return value


def validate_integration_intent(
    *,
    repo: Path,
    metadata_root: Path,
    intent: dict[str, Any],
    repository_id: str,
    allocation_id: str,
    lease_id: str,
    candidate: str,
    expected_main: str,
    evidence: str,
) -> None:
    if (
        intent.get("allocation_id") != allocation_id
        or intent.get("candidate_oid") != candidate
        or intent.get("validated_oid") != candidate
        or intent.get("validation_evidence_id") != evidence
    ):
        raise PublishError("integration intent validation differs from the lease")

    # Pre-0.6 Controller intents are frozen in VALIDATED. Controller 0.6 V2 moves the
    # same exact candidate to PUBLISHING while the lease is live and binds that state
    # to the lease UUID. Do not accept either state outside its own contract.
    if intent.get("state") == "VALIDATED":
        if intent.get("schema_version") not in {None, 1, 2} or intent.get(
            "contract_version"
        ) not in {None, 1}:
            raise PublishError("legacy integration intent contract is inconsistent")
        return

    if (
        intent.get("schema_version") != 3
        or intent.get("contract_version") != 2
        or intent.get("state") != "PUBLISHING"
        or require_uuid(intent.get("publication_identity"), "publication identity")
        != lease_id
        or intent.get("base_main_oid") != expected_main
        or intent.get("prepared_base_origin_main_oid") != expected_main
        or intent.get("prepared_commit_oid") != candidate
    ):
        raise PublishError("Controller V2 publishing intent differs from the lease")

    intent_id = require_uuid(intent.get("intent_id"), "intent_id")
    prepared_receipt_id = require_uuid(
        intent.get("prepared_receipt_id"), "prepared receipt ID"
    )
    prepare_identity = require_sha256(
        intent.get("prepare_identity"), "prepare identity"
    )
    scope_digest = require_sha256(intent.get("scope_digest"), "scope digest")
    prepared_scope_digest = require_sha256(
        intent.get("prepared_scope_digest"), "prepared scope digest"
    )
    if prepared_scope_digest != scope_digest:
        raise PublishError("prepared scope digest differs from the integration intent")
    prepared_tree = require_oid(intent.get("prepared_tree_oid"), "prepared tree OID")
    live_tree = require_oid(
        git_text(repo, "rev-parse", f"{candidate}^{{tree}}"), "candidate tree OID"
    )
    if prepared_tree != live_tree:
        raise PublishError("prepared tree differs from the leased candidate")
    tests_receipt_id = require_text(intent.get("tests_receipt_id"), "tests receipt ID")
    snapshot_receipt_id = require_text(
        intent.get("snapshot_receipt_id"), "snapshot receipt ID"
    )
    attempt = require_positive_int(intent.get("attempt"), "integration attempt")

    prepared = read_object(
        metadata_root
        / "prepared-candidate-receipts"
        / f"{prepared_receipt_id}.json",
        "prepared candidate receipt",
    )
    expected_fields = {
        "schema_version",
        "receipt_id",
        "repository_id",
        "intent_id",
        "allocation_id",
        "attempt",
        "base_origin_main_oid",
        "candidate_oid",
        "tree_oid",
        "scope_digest",
        "prepare_identity",
        "validation_evidence_id",
        "tests_receipt_id",
        "snapshot_receipt_id",
        "created_at",
    }
    if set(prepared) != expected_fields or prepared.get("schema_version") != 1:
        raise PublishError("prepared candidate receipt schema is invalid")
    if (
        require_uuid(prepared.get("receipt_id"), "prepared receipt ID")
        != prepared_receipt_id
        or require_uuid(prepared.get("repository_id"), "prepared repository ID")
        != repository_id
        or require_uuid(prepared.get("intent_id"), "prepared intent ID") != intent_id
        or require_uuid(prepared.get("allocation_id"), "prepared allocation ID")
        != allocation_id
        or require_positive_int(prepared.get("attempt"), "prepared attempt") != attempt
        or prepared.get("base_origin_main_oid") != expected_main
        or prepared.get("candidate_oid") != candidate
        or prepared.get("tree_oid") != prepared_tree
        or prepared.get("scope_digest") != scope_digest
        or prepared.get("prepare_identity") != prepare_identity
        or prepared.get("validation_evidence_id") != evidence
        or prepared.get("tests_receipt_id") != tests_receipt_id
        or prepared.get("snapshot_receipt_id") != snapshot_receipt_id
    ):
        raise PublishError("prepared candidate receipt differs from the publishing intent")


def resolve_repo(raw: Path) -> tuple[Path, Path, Path]:
    if not raw.is_absolute():
        raise PublishError("--repo must be absolute")
    try:
        requested = raw.resolve(strict=True)
    except OSError as exc:
        raise PublishError("--repo is unavailable") from exc
    root = Path(git_text(requested, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if root != requested or not root.is_dir():
        raise PublishError("--repo must equal the candidate worktree root")
    common = Path(
        git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve(strict=True)
    git_dir = Path(
        git_text(root, "rev-parse", "--path-format=absolute", "--absolute-git-dir")
    ).resolve(strict=True)
    if git_dir == common:
        raise PublishError("canonical checkout cannot publish an integration candidate")
    return root, common, git_dir


def worktree_key(common: Path, git_dir: Path) -> str:
    try:
        relative = git_dir.relative_to(common)
    except ValueError as exc:
        raise PublishError(
            "candidate Git directory is outside the common repository"
        ) from exc
    return "wt_" + hashlib.sha256(os.fspath(relative).encode()).hexdigest()


def validate_branch_name(repo: Path, branch: str) -> None:
    result = run_git(repo, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        raise PublishError("lease target branch is invalid")


def live_remote_oid(repo: Path, remote: str, target_ref: str) -> str:
    result = run_git(repo, "ls-remote", "--refs", remote, target_ref, check=False)
    if result.returncode != 0:
        raise PublishError("cannot read the live publication target")
    lines = [line for line in result.stdout.splitlines() if line]
    if len(lines) != 1:
        raise PublishError("live publication target is missing or ambiguous")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[1] != target_ref:
        raise PublishError("live publication target response is malformed")
    return require_oid(fields[0], "live remote OID")


def validate_lease(
    *,
    repo: Path,
    common: Path,
    git_dir: Path,
    metadata_root: Path,
    candidate_argument: str,
    lease_id_argument: str,
    run_id_argument: str,
) -> dict[str, Any]:
    lease = read_object(metadata_root / "publication-lease.json", "publication lease")
    expected_fields = {
        "schema_version",
        "lease_id",
        "repository_id",
        "allocation_id",
        "holder",
        "run_id",
        "remote",
        "branch",
        "target_ref",
        "candidate_oid",
        "expected_main_oid",
        "validation_evidence_id",
        "acquired_at",
        "expires_at",
    }
    if set(lease) != expected_fields or lease.get("schema_version") != 2:
        raise PublishError("publication lease schema is not Controller v2")
    if require_uuid(lease.get("lease_id"), "lease_id") != lease_id_argument:
        raise PublishError("publication lease ID differs from the invoking run")
    run_id = require_text(lease.get("run_id"), "lease run_id", maximum=256)
    if run_id != run_id_argument:
        raise PublishError("publication run identity differs from the lease")
    candidate = require_oid(lease.get("candidate_oid"), "candidate_oid")
    if candidate != candidate_argument:
        raise PublishError("candidate argument differs from the leased candidate")
    expected_main = require_oid(lease.get("expected_main_oid"), "expected_main_oid")
    repository_id = require_uuid(lease.get("repository_id"), "repository_id")
    allocation_id = require_uuid(lease.get("allocation_id"), "allocation_id")
    holder = require_text(lease.get("holder"), "lease holder")
    evidence = require_text(lease.get("validation_evidence_id"), "validation evidence")
    remote = require_text(lease.get("remote"), "lease remote", maximum=128)
    if SAFE_REMOTE.fullmatch(remote) is None:
        raise PublishError("lease remote name is unsafe")
    branch = require_text(lease.get("branch"), "lease branch", maximum=256)
    validate_branch_name(repo, branch)
    target_ref = require_text(lease.get("target_ref"), "target_ref", maximum=512)
    if target_ref != f"refs/heads/{branch}":
        raise PublishError("lease target ref and branch differ")

    repo_metadata = read_object(metadata_root / "repo.json", "repository metadata")
    if (
        require_uuid(repo_metadata.get("repository_id"), "repository metadata ID")
        != repository_id
    ):
        raise PublishError("lease repository identity differs from Controller metadata")
    binding = read_object(
        metadata_root / "bindings" / f"{worktree_key(common, git_dir)}.json",
        "candidate binding",
    )
    if binding.get("allocation_id") != allocation_id:
        raise PublishError("candidate binding differs from the leased allocation")
    record = read_object(
        metadata_root / "records" / f"{allocation_id}.json", "candidate allocation"
    )
    if (
        record.get("allocation_id") != allocation_id
        or record.get("role") != "integration"
        or record.get("lifecycle") != "INTEGRATING"
        or record.get("owner") != holder
        or record.get("task_commit") != candidate
        or record.get("handoff_commit") != candidate
        or record.get("finalizer_record_id") != evidence
        or record.get("finalizer_commit") != candidate
    ):
        raise PublishError("leased candidate allocation evidence is inconsistent")
    intents_root = metadata_root / "integration-intents"
    matching_intents = []
    if intents_root.is_dir() and not intents_root.is_symlink():
        for path in sorted(intents_root.glob("*.json")):
            intent = read_object(path, "integration intent")
            if intent.get("allocation_id") == allocation_id:
                matching_intents.append(intent)
    if len(matching_intents) == 1:
        validate_integration_intent(
            repo=repo,
            metadata_root=metadata_root,
            intent=matching_intents[0],
            repository_id=repository_id,
            allocation_id=allocation_id,
            lease_id=lease_id_argument,
            candidate=candidate,
            expected_main=expected_main,
            evidence=evidence,
        )
    else:
        raise PublishError("exactly one frozen integration intent must bind the lease")

    head = require_oid(git_text(repo, "rev-parse", "HEAD"), "candidate HEAD")
    if head != candidate:
        raise PublishError("candidate checkout HEAD differs from the lease")
    symbolic = run_git(repo, "symbolic-ref", "--quiet", "HEAD", check=False)
    if symbolic.returncode != 0 or symbolic.stdout.strip() == target_ref:
        raise PublishError("candidate must be attached to a non-publication branch")
    if run_git(repo, "status", "--porcelain=v1", "-z").stdout:
        raise PublishError("candidate checkout must be clean before publication")
    ancestry = run_git(
        repo,
        "merge-base",
        "--is-ancestor",
        expected_main,
        candidate,
        check=False,
    )
    if ancestry.returncode != 0:
        raise PublishError("candidate does not contain the leased expected main OID")
    fetch_urls = run_git(repo, "remote", "get-url", "--all", remote, check=False)
    push_urls = run_git(
        repo, "remote", "get-url", "--push", "--all", remote, check=False
    )
    if (
        fetch_urls.returncode != 0
        or push_urls.returncode != 0
        or not fetch_urls.stdout.strip()
        or "\n" in fetch_urls.stdout.strip()
        or fetch_urls.stdout.strip() != push_urls.stdout.strip()
    ):
        raise PublishError(
            "lease remote must have one identical fetch and push endpoint"
        )
    return lease


def is_expired(value: object) -> bool:
    from datetime import datetime, timezone

    text = require_text(value, "lease expires_at", maximum=128)
    try:
        expires = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PublishError("lease expires_at is invalid") from exc
    if expires.tzinfo is None:
        raise PublishError("lease expires_at must include a timezone")
    return expires <= datetime.now(timezone.utc)


def publish(arguments: argparse.Namespace) -> dict[str, Any]:
    candidate = require_oid(
        arguments.publish_integration_candidate, "candidate argument"
    )
    lease_id = require_uuid(arguments.lease_id, "lease argument")
    run_id = require_text(arguments.run_id, "run argument", maximum=256)
    repo, common, git_dir = resolve_repo(arguments.repo)
    # GF-INTEGRATION-ADAPTER: worktree-controller-integration-publication (single site)
    metadata_root = common / "worktree-controller" / "v1"
    lock_path = metadata_root / "repo.lock"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise PublishError("Controller repository lock is missing or unsafe")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    push_executed = False
    try:
        # Exclusive flock is the existing Controller repository lock. It serializes duplicate
        # executors for one lease as well as lease revocation/replacement.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        lease = validate_lease(
            repo=repo,
            common=common,
            git_dir=git_dir,
            metadata_root=metadata_root,
            candidate_argument=candidate,
            lease_id_argument=lease_id,
            run_id_argument=run_id,
        )
        remote = str(lease["remote"])
        target_ref = str(lease["target_ref"])
        expected_main = str(lease["expected_main_oid"])
        observed = live_remote_oid(repo, remote, target_ref)
        if observed == candidate:
            result = "already_published_recovered"
        else:
            if is_expired(lease.get("expires_at")):
                raise PublishError("publication lease expired before the mutation gate")
            if observed != expected_main:
                raise PublishError(
                    "live main OID differs from the leased expected main OID"
                )
            push = run_git(
                repo,
                "push",
                "--porcelain",
                "--no-follow-tags",
                remote,
                f"{candidate}:{target_ref}",
                check=False,
            )
            push_executed = True
            if os.environ.get("GIT_FINALIZER_TEST_CRASH_AFTER_PUSH") == "1":
                os._exit(97)
            if push.returncode != 0:
                observed_after_failure = live_remote_oid(repo, remote, target_ref)
                if observed_after_failure != candidate:
                    raise PublishError(f"integration push failed (exit {push.returncode}); remote candidate was not verified")
            verified = live_remote_oid(repo, remote, target_ref)
            if verified != candidate:
                raise PublishError(
                    "remote verification does not equal the exact candidate OID"
                )
            result = "published"
        verified = live_remote_oid(repo, remote, target_ref)
        if verified != candidate:
            raise PublishError("remote fact changed before publication receipt output")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    record_id = f"git-finalizer-integration-{lease_id}-{candidate}"
    return {
        "summary_schema_version": 1,
        "finalizer_version": VERSION,
        "mode": "integration_candidate_publish",
        "status": "success",
        "final_phase": "remote_verified",
        "repository": str(repo),
        "repository_id": lease["repository_id"],
        "allocation_id": lease["allocation_id"],
        "lease_id": lease_id,
        "run_id": run_id,
        "target_ref": lease["target_ref"],
        "expected_main_oid": lease["expected_main_oid"],
        "candidate_oid": candidate,
        "record_id": record_id,
        "push": {
            "executed": push_executed,
            "result": result,
            "branch_refspec_only": True,
            "follow_tags_requested": False,
        },
        "remote_verify": {"status": "verified", "oid": verified},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--publish-integration-candidate", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary", action="store_true")
    return parser


def fail(exc: Exception, *, summary: bool) -> NoReturn:
    if summary:
        print(
            json.dumps(
                {
                    "summary_schema_version": 1,
                    "finalizer_version": VERSION,
                    "mode": "integration_candidate_publish",
                    "status": "blocked",
                    "final_phase": "pre_publish_or_verify",
                    "reason": str(exc)[:512],
                    "next_action": "read_remote_fact_and_reenter_controller_publish_gate",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    summary = "--summary" in raw
    try:
        arguments = build_parser().parse_args(raw)
        result = publish(arguments)
    except (PublishError, OSError, UnicodeError, ValueError) as exc:
        fail(exc, summary=summary)
    if arguments.summary:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"publication={result['push']['result']}")
        print(f"candidate_oid={result['candidate_oid']}")
        print(f"remote_verified={result['remote_verify']['oid']}")
        print(f"finalizer_record_id={result['record_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
