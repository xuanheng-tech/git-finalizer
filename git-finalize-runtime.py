#!/usr/bin/env python3
"""Named state serialization for the Git Finalizer lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any

SUMMARY_FIELDS = frozenset({
    'SUMMARY_SCHEMA_VERSION',
    'VERSION',
    'summary_mode',
    'summary_status',
    'summary_phase',
    'summary_exit_code',
    'summary_repo',
    'summary_repository_id',
    'summary_allocation_id',
    'summary_task_key',
    'summary_authority_key',
    'summary_worktree_path',
    'summary_role',
    'summary_branch',
    'summary_upstream',
    'summary_path_count',
    'summary_dry_run',
    'summary_commit_created',
    'summary_commit_sha',
    'summary_push_executed',
    'summary_push_result',
    'summary_push_branch_refspec_only',
    'summary_push_follow_tags_disabled',
    'summary_post_verify',
    'summary_reason',
    'summary_next_action',
    'summary_warnings_omitted',
    'summary_mode_state',
    'summary_snapshot_verification',
    'summary_remote_conclusion',
    'summary_recovery_start',
    'summary_commit_reused',
    'summary_completed_through',
    'summary_resume_required',
    'summary_resume_oid',
    'summary_resume_remote',
    'summary_worktree_state',
    'summary_worktree_before_state',
    'summary_commit_result',
    'summary_head_before',
    'summary_head_after',
    'summary_head_unchanged',
    'summary_index_unchanged',
    'summary_worktree_unchanged',
    'summary_resume_publish_kind',
    'summary_publish_ahead',
    'summary_publish_behind',
    'summary_publish_commit_count',
    'summary_publish_range',
    'summary_push_target',
    'summary_final_head',
    'summary_final_remote_oid',
    'summary_sync_source_remote',
    'summary_sync_expected_target_oid',
    'summary_retirement_operation',
    'summary_retirement_remote',
    'summary_retirement_branch',
    'summary_retirement_ref',
    'summary_retirement_expected_oid',
    'summary_retirement_integrated_ref',
    'summary_retirement_integrated_oid',
    'summary_retirement_ancestry',
    'summary_retirement_ci_required',
    'summary_retirement_ci_status',
    'summary_retirement_ci_commit_oid',
    'summary_retirement_ci_source',
    'summary_retirement_ci_verified_at',
    'summary_retirement_remote_absent_after',
    'summary_retirement_result',
    'summary_retirement_lease_bound',
    'summary_retirement_tags_unchanged',
    'summary_retirement_remote_tracking_absent',
    'summary_retirement_local_absent_after',
    'summary_retirement_plan_id',
    'summary_retirement_receipt_id',
    'summary_fixture_exceptions',
    'summary_source_reviews',
    'summary_runtime_identity',
    'summary_diagnostics_requested',
    'summary_scan_path',
    'summary_scan_stage',
    'summary_scan_verdict',
    'summary_scan_size',
    'summary_scan_binary',
    'summary_scan_sha256',
    'summary_scan_blob_oid',
    'summary_scanner_sha256',
})


def summary_arguments(arguments: list[str]) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    warnings: list[str] = []
    for argument in arguments:
        name, separator, value = argument.partition("=")
        if not separator:
            raise ValueError("summary field has no value separator")
        if name == "warning":
            warnings.append(value)
        elif name not in SUMMARY_FIELDS or name in fields:
            raise ValueError("unknown or duplicate summary field")
        else:
            fields[name] = value
    if set(fields) != SUMMARY_FIELDS or len(warnings) > 5:
        raise ValueError("incomplete summary state")
    return fields, warnings


def render_summary(fields: dict[str, str], warning_values: list[str]) -> dict[str, Any]:
    def boolean(value):
        return value == "true"


    def bounded(value, limit):
        omitted = max(0, len(value) - limit)
        return value[:limit], omitted


    schema_version = int(fields['SUMMARY_SCHEMA_VERSION'])
    version = fields['VERSION']
    mode = fields['summary_mode']
    status = fields['summary_status']
    phase = fields['summary_phase']
    exit_code = int(fields['summary_exit_code'])
    repository = fields['summary_repo']
    repository_id = fields['summary_repository_id']
    allocation_id = fields['summary_allocation_id']
    task_key = fields['summary_task_key']
    authority_key = fields['summary_authority_key']
    worktree_path = fields['summary_worktree_path']
    role = fields['summary_role']
    branch = fields['summary_branch']
    upstream = fields['summary_upstream']
    path_count = int(fields['summary_path_count'])
    dry_run = boolean(fields['summary_dry_run'])
    commit_created = boolean(fields['summary_commit_created'])
    commit_sha = fields['summary_commit_sha']
    push_executed = boolean(fields['summary_push_executed'])
    push_result = fields['summary_push_result']
    push_branch_refspec_only_raw = fields['summary_push_branch_refspec_only']
    push_follow_tags_disabled_raw = fields['summary_push_follow_tags_disabled']
    push_branch_refspec_only = (
        boolean(push_branch_refspec_only_raw) if push_branch_refspec_only_raw else None
    )
    push_follow_tags_requested = (
        False if boolean(push_follow_tags_disabled_raw) else None
    )
    post_verify = fields['summary_post_verify']
    reason, reason_omitted = bounded(fields['summary_reason'], 512)
    next_action, next_action_omitted = bounded(fields['summary_next_action'], 256)
    warnings_omitted = int(fields['summary_warnings_omitted'])
    mode_state = fields['summary_mode_state']
    snapshot_verification = fields['summary_snapshot_verification']
    remote_conclusion = fields['summary_remote_conclusion']
    recovery_start = fields['summary_recovery_start']
    commit_reused = boolean(fields['summary_commit_reused'])
    completed_through = fields['summary_completed_through']
    resume_required = boolean(fields['summary_resume_required'])
    resume_oid = fields['summary_resume_oid']
    resume_remote = fields['summary_resume_remote']
    worktree_state = fields['summary_worktree_state']
    worktree_before_state = fields['summary_worktree_before_state']
    commit_result = fields['summary_commit_result']
    head_before = fields['summary_head_before']
    head_after = fields['summary_head_after']
    head_unchanged = boolean(fields['summary_head_unchanged'])
    index_unchanged = boolean(fields['summary_index_unchanged'])
    worktree_unchanged = boolean(fields['summary_worktree_unchanged'])
    resume_publish_kind = fields['summary_resume_publish_kind']
    publish_ahead_raw = fields['summary_publish_ahead']
    publish_behind_raw = fields['summary_publish_behind']
    publish_commit_count_raw = fields['summary_publish_commit_count']
    publish_range = fields['summary_publish_range']
    push_target = fields['summary_push_target']
    final_head = fields['summary_final_head']
    final_remote_oid = fields['summary_final_remote_oid']
    sync_source_remote = fields['summary_sync_source_remote']
    sync_expected_target_oid = fields['summary_sync_expected_target_oid']
    retirement_operation = fields['summary_retirement_operation']
    retirement_remote = fields['summary_retirement_remote']
    retirement_branch = fields['summary_retirement_branch']
    retirement_ref = fields['summary_retirement_ref']
    retirement_expected_oid = fields['summary_retirement_expected_oid']
    retirement_integrated_ref = fields['summary_retirement_integrated_ref']
    retirement_integrated_oid = fields['summary_retirement_integrated_oid']
    retirement_ancestry_raw = fields['summary_retirement_ancestry']
    retirement_ci_required = boolean(fields['summary_retirement_ci_required'])
    retirement_ci_status = fields['summary_retirement_ci_status']
    retirement_ci_commit_oid = fields['summary_retirement_ci_commit_oid']
    retirement_ci_source = fields['summary_retirement_ci_source']
    retirement_ci_verified_at = fields['summary_retirement_ci_verified_at']
    retirement_remote_absent_after_raw = fields['summary_retirement_remote_absent_after']
    retirement_result = fields['summary_retirement_result']
    retirement_lease_bound = boolean(fields['summary_retirement_lease_bound'])
    retirement_tags_unchanged = boolean(fields['summary_retirement_tags_unchanged'])
    retirement_remote_tracking_absent = boolean(fields['summary_retirement_remote_tracking_absent'])
    retirement_local_absent_after_raw = fields['summary_retirement_local_absent_after']
    retirement_plan_id = fields['summary_retirement_plan_id']
    retirement_receipt_id = fields['summary_retirement_receipt_id']
    fixture_exceptions = [json.loads(line) for line in fields['summary_fixture_exceptions'].splitlines() if line]
    source_reviews = [json.loads(line) for line in fields['summary_source_reviews'].splitlines() if line]
    runtime_raw = fields['summary_runtime_identity']
    runtime = json.loads(runtime_raw) if runtime_raw else None
    diagnostics_requested = fields['summary_diagnostics_requested'] == "1"
    scan_path = fields['summary_scan_path']
    scan_stage = fields['summary_scan_stage']
    scan_verdict = fields['summary_scan_verdict']
    scan_size = fields['summary_scan_size']
    scan_binary = fields['summary_scan_binary']
    scan_sha256 = fields['summary_scan_sha256']
    scan_blob_oid = fields['summary_scan_blob_oid']
    scanner_sha256 = fields['summary_scanner_sha256']


    def optional_integer(value):
        return int(value) if value else None


    def optional_boolean(value):
        return boolean(value) if value else None

    warnings = []
    warning_characters_omitted = 0
    for raw_warning in warning_values:
        warning, omitted = bounded(raw_warning, 256)
        warnings.append(warning)
        warning_characters_omitted += omitted

    result = {
        "summary_schema_version": schema_version,
        "finalizer_version": version,
        "mode": mode,
        "status": status,
        "final_phase": phase,
        "exit_code": exit_code,
        "repository": repository or None,
        "repository_id": repository_id or None,
        "allocation_id": allocation_id or None,
        "task_key": task_key or None,
        "authority_key": authority_key or None,
        "worktree_path": worktree_path or None,
        "role": role or None,
        "branch": branch or None,
        "upstream": upstream or None,
        "requested_path_count": path_count,
        "dry_run": dry_run,
        "commit": {"created": commit_created, "sha": commit_sha or None},
        "push": {
            "executed": push_executed,
            "result": push_result,
            "branch_refspec_only": push_branch_refspec_only,
            "follow_tags_requested": push_follow_tags_requested,
        },
        "warnings": warnings,
        "warnings_omitted": warnings_omitted,
        "warning_characters_omitted": warning_characters_omitted,
        "reason": reason or None,
        "reason_omitted_characters": reason_omitted,
        "next_action": next_action,
        "next_action_omitted_characters": next_action_omitted,
    }
    if fixture_exceptions:
        result["fixture_exceptions"] = fixture_exceptions
    if source_reviews:
        if status == "success" and not dry_run:
            for review in source_reviews:
                review["content_scan"] = "passed"
        result["reviewed_sensitive_sources"] = source_reviews
    if mode == "initial":
        result["mode_result"] = {
            "initial_publish": mode_state,
            "snapshot_verification": snapshot_verification,
            "remote_conclusion": remote_conclusion,
            "post_verify": post_verify,
        }
    elif mode == "initial_branch":
        result["mode_result"] = {
            "initial_branch_publish": mode_state,
            "remote_conclusion": remote_conclusion,
            "post_verify": post_verify,
        }
    elif mode in {"publish_existing_branch", "publish_existing_history"}:
        interface = (
            "publish-existing-history"
            if mode == "publish_existing_history"
            else "publish-existing-branch"
        )
        result["mode_result"] = {
            "interface": interface,
            "commit_reused": commit_reused,
            "remote_conclusion": remote_conclusion,
            "pre_publish": {
                "commit_count": optional_integer(publish_commit_count_raw),
                "commit_range": publish_range or None,
            },
            "push_target": push_target or None,
            "post_verify": post_verify,
            "head": {
                "before": head_before or None,
                "after": head_after or None,
                "unchanged": head_unchanged,
            },
            "index_unchanged": index_unchanged,
            "worktree": {
                "before": worktree_before_state or None,
                "after": worktree_state or None,
                "unchanged": worktree_unchanged,
            },
            "final": {
                "head": final_head or None,
                "remote_ref_oid": final_remote_oid or None,
            },
        }
        if mode == "publish_existing_history":
            result["mode_result"]["bootstrap_prerequisite"] = "repo-plan/repo-ensure"
    elif mode == "sync_published_branch":
        result["mode_result"] = {
            "interface": "sync-published-branch",
            "source_remote": sync_source_remote or None,
            "published_oid": commit_sha or None,
            "expected_target_oid": sync_expected_target_oid or None,
            "push_target": push_target or None,
            "commit_count": optional_integer(publish_commit_count_raw),
            "post_verify": post_verify,
            "remote_conclusion": remote_conclusion,
            "final_remote_oid": final_remote_oid or None,
            "local_head_unchanged": head_unchanged,
            "local_upstream_unchanged": head_unchanged and index_unchanged and worktree_unchanged,
        }
    elif mode == "resume":
        resume_interface = {
            "initial_root": "resume-initial-publish",
            "existing_commits": "resume-publish",
            "existing_history_first": "resume-existing-history-publish",
        }.get(resume_publish_kind)
        result["mode_result"] = {
            "interface": resume_interface,
            "publish_kind": resume_publish_kind or None,
            "recovery_start": recovery_start or None,
            "commit_reused": commit_reused,
            "completed_through": completed_through,
            "resume_required": resume_required,
            "pre_publish": {
                "ahead": optional_integer(publish_ahead_raw),
                "behind": optional_integer(publish_behind_raw),
                "commit_count": optional_integer(publish_commit_count_raw),
                "commit_range": publish_range or None,
            },
            "push_target": push_target or None,
            "post_verify": post_verify,
            "final": {
                "head": final_head or None,
                "remote_ref_oid": final_remote_oid or None,
                "worktree": worktree_state or None,
            },
        }
    elif mode == "initial-commit-only":
        result["mode_result"] = {
            "initial_root_commit": mode_state,
            "remote_verification": remote_conclusion,
            "post_verify": post_verify,
            "worktree": worktree_state or None,
        }
    elif mode == "commit-only":
        result["mode_result"] = {
            "remote_verification": remote_conclusion,
            "post_verify": post_verify,
            "worktree": worktree_state or None,
        }
    elif mode == "verify-only":
        result["mode_result"] = {
            "local_validation": mode_state,
            "commit": commit_result,
            "remote_verification": remote_conclusion,
            "post_verify": post_verify,
            "head": {
                "before": head_before or None,
                "after": head_after or None,
                "unchanged": head_unchanged,
            },
            "index_unchanged": index_unchanged,
            "worktree": {
                "before": worktree_before_state or None,
                "after": worktree_state or None,
                "unchanged": worktree_unchanged,
            },
        }
    elif mode in {"retire_local_branch", "retire_remote_branch"}:
        operation_name = "local" if mode == "retire_local_branch" else "remote"
        result["mode_result"] = {
            "interface": f"retire-{operation_name}-branch",
            "operation": retirement_operation or None,
            "remote": retirement_remote or None,
            "branch": retirement_branch or None,
            f"{operation_name}_ref": retirement_ref or None,
            f"expected_{operation_name}_oid": retirement_expected_oid or None,
            "expected_oid": retirement_expected_oid or None,
            "retirement_plan_id": retirement_plan_id or None,
            "integrated_into_ref": retirement_integrated_ref or None,
            "integrated_into_oid": retirement_integrated_oid or None,
            "ancestry_verified": optional_boolean(retirement_ancestry_raw),
            "ci_required": retirement_ci_required,
            "ci_status": retirement_ci_status,
            "ci_commit_oid": retirement_ci_commit_oid or None,
            "ci_verification_source": retirement_ci_source,
            "ci_verified_at": retirement_ci_verified_at or None,
            "expected_oid_lease_bound": retirement_lease_bound,
            "remote_absent_after": optional_boolean(retirement_remote_absent_after_raw),
            "local_absent_after": optional_boolean(retirement_local_absent_after_raw),
            "remote_tracking_ref_absent": retirement_remote_tracking_absent,
            "head": {
                "before": head_before or None,
                "after": head_after or None,
                "unchanged": head_unchanged,
            },
            "index_unchanged": index_unchanged,
            "worktree": {
                "clean_before": worktree_before_state == "clean",
                "clean_after": worktree_state == "clean",
                "unchanged": worktree_unchanged,
            },
            "tags_unchanged": retirement_tags_unchanged,
            "result": retirement_result,
        }
        if retirement_receipt_id:
            result["mode_result"]["retirement_receipt_id"] = retirement_receipt_id
    else:
        result["mode_result"] = {"post_verify": post_verify}
    if resume_required:
        result["resume"] = {
            "head_oid": resume_oid,
            "remote": resume_remote,
        }
    if runtime is not None and (diagnostics_requested or scan_verdict):
        if scanner_sha256:
            runtime["content_scanner"]["sha256"] = scanner_sha256
        content_scan = None
        if scan_verdict:
            detectors = {"PRIVATE": "private-key-header-v1", "KNOWN": "known-token-v1",
                         "SSH": "ssh-key", "ASSIGNMENT": "credential-assignment-v1",
                         "ERROR": "content-scan-error"}
            content_scan = {"path": scan_path, "stage": scan_stage,
                            "verdict": scan_verdict, "detector": detectors[scan_verdict],
                            "byte_length": optional_integer(scan_size),
                            "binary": optional_boolean(scan_binary),
                            "sha256": scan_sha256 or None, "blob_oid": scan_blob_oid or None}
        result["diagnostics"] = {"schema_version": 1, "runtime": runtime,
                                 "content_scan": content_scan}

    return result



def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "locked-entrypoint":
        try:
            return locked_entrypoint(Path(sys.argv[2]), sys.argv[3:])
        except (OSError, ValueError):
            print("ERROR: Git Finalizer execution lock is unavailable", file=sys.stderr)
            return 1
    if len(sys.argv) < 2 or sys.argv[1] != "summary":
        return 1
    try:
        fields, warnings = summary_arguments(sys.argv[2:])
        result = render_summary(fields, warnings)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        return 1


def locked_entrypoint(entrypoint: Path, arguments: list[str]) -> int:
    entrypoint = entrypoint.resolve(strict=True)
    lock_path = entrypoint.with_name(".git-finalizer-execution.lock")
    descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe execution lock")
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("execution lock wait exceeded")
                time.sleep(0.05)
        # The shell inherits the lock so an interrupted launcher cannot unlock
        # an invocation that is still using the installed companions.
        result = subprocess.run(
            ["bash", str(entrypoint), *arguments], check=False, pass_fds=(descriptor,),
            env={**os.environ, "GF_EXECUTION_LOCK_ENTRY": str(entrypoint)},
        )
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
