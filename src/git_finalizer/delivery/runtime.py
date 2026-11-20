"""Machine-readable `--summary` serialization and child-safe execution lock."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import stat
import time
from typing import Any

from git_finalizer import __version__
from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.json_io import sha256_file

SUMMARY_SCHEMA_VERSION = 1
EXECUTION_LOCK_NAME = ".git-finalizer-execution.lock"

FIXED_SUMMARY_KEYS = (
    "summary_schema_version",
    "finalizer_version",
    "mode",
    "status",
    "final_phase",
    "exit_code",
    "repository",
    "repository_id",
    "allocation_id",
    "task_key",
    "authority_key",
    "worktree_path",
    "role",
    "branch",
    "upstream",
    "requested_path_count",
    "dry_run",
    "commit",
    "push",
    "warnings",
    "warnings_omitted",
    "warning_characters_omitted",
    "reason",
    "reason_omitted_characters",
    "next_action",
    "next_action_omitted_characters",
    "mode_result",
)

CONDITIONAL_SUMMARY_KEYS = (
    "error_code",
    "resume",
    "fixture_exceptions",
    "reviewed_sensitive_sources",
    "diagnostics",
)

ALLOWED_STATUSES = frozenset({"success", "blocked", "failed"})

ALLOWED_NEXT_ACTIONS = frozenset(
    {
        "none",
        "resolve_blocker_and_retry",
        "inspect_failure",
        "inspect_failure_with_local_commit_retained",
        "inspect_local_root_commit",
        "inspect_existing_branch_publish_failure",
        "inspect_initial_branch_publish_failure",
        "resume_initial_publish",
        "resume_existing_history_publish",
        "retry_same_resume",
        "inspect_remote_state_then_rerun_same_retirement",
        "rerun_without_summary_for_diagnostics",
        "run_without_dry_run_after_review",
        "publish_install_verify_then_run_without_dry_run",
    }
)


def _limit_text(value: str | None, maximum: int) -> tuple[str | None, int]:
    if value is None or value == "":
        return None, 0
    normalized = " ".join(value.replace("\r", "\n").splitlines()).strip()
    if not normalized:
        return None, 0
    if len(normalized) <= maximum:
        return normalized, 0
    if maximum <= 3:
        return normalized[:maximum], len(normalized) - maximum
    return normalized[: maximum - 3] + "...", len(normalized) - (maximum - 3)


def _parse_warnings(values: list[str]) -> tuple[list[str], int, int]:
    bounded: list[str] = []
    omitted_chars = 0
    for item in values[:5]:
        text, clipped = _limit_text(item, 240)
        if text is not None:
            bounded.append(text)
        omitted_chars += clipped
    return bounded, max(0, len(values) - 5), omitted_chars


def runtime_identity(entrypoint: Path | None = None) -> dict[str, dict[str, str]]:
    """Return byte identity of the active CLI entry and pure-Python content scanner."""
    from git_finalizer.delivery import content_scan

    resolved_entry = (entrypoint or Path(__file__)).resolve()
    scanner_path = Path(content_scan.__file__).resolve()
    result: dict[str, dict[str, str]] = {}
    if resolved_entry.is_file():
        result["entrypoint"] = {
            "path": str(resolved_entry),
            "sha256": sha256_file(resolved_entry),
        }
    if scanner_path.is_file():
        result["content_scanner"] = {
            "path": str(scanner_path),
            "sha256": sha256_file(scanner_path),
        }
    return result


@dataclasses.dataclass
class SummaryState:
    """Explicit named state backing the `--summary` machine contract."""

    mode: str = "normal"
    status: str = "failed"
    final_phase: str = "preflight"
    exit_code: int = 1
    repository: str = ""
    repository_id: str | None = None
    allocation_id: str | None = None
    task_key: str | None = None
    authority_key: str | None = None
    worktree_path: str | None = None
    role: str | None = None
    branch: str | None = None
    upstream: str | None = None
    requested_path_count: int = 0
    dry_run: bool = False
    commit_created: bool = False
    commit_sha: str | None = None
    commit_result: str = "not_run"
    push_executed: bool = False
    push_result: str = "not_run"
    push_branch_refspec_only: bool | None = None
    push_follow_tags_requested: bool | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)
    warnings_omitted_extra: int = 0
    reason: str | None = None
    next_action: str = "inspect_failure"
    error_code: str | None = None
    mode_state: str = "not_started"
    remote_conclusion: str = "not_run"
    post_verify: str = "not_run"
    worktree_state: str | None = None
    worktree_before_state: str | None = None
    worktree_unchanged: bool | None = None
    head_before: str | None = None
    head_after: str | None = None
    head_unchanged: bool | None = None
    index_unchanged: bool | None = None
    snapshot_verification: str = "not_requested"
    commit_reused: bool | None = None
    completed_through: str | None = None
    resume_publish_kind: str | None = None
    recovery_start: str | None = None
    resume_required: bool = False
    resume_oid: str | None = None
    resume_remote: str | None = None
    publish_ahead: int | None = None
    publish_behind: int | None = None
    publish_commit_count: int | None = None
    publish_range: str | None = None
    push_target: str | None = None
    final_head: str | None = None
    final_remote_oid: str | None = None
    sync_source_remote: str | None = None
    sync_expected_target_oid: str | None = None
    retirement_operation: str | None = None
    retirement_remote: str | None = None
    retirement_branch: str | None = None
    retirement_ref: str | None = None
    retirement_expected_oid: str | None = None
    retirement_plan_id: str | None = None
    retirement_integrated_ref: str | None = None
    retirement_integrated_oid: str | None = None
    retirement_ancestry_verified: bool | None = None
    retirement_ci_required: bool = False
    retirement_ci_status: str | None = None
    retirement_ci_commit_oid: str | None = None
    retirement_ci_source: str = "not_required"
    retirement_ci_verified_at: str | None = None
    retirement_lease_bound: bool | None = None
    retirement_remote_absent_after: bool | None = None
    retirement_local_absent_after: bool | None = None
    retirement_remote_tracking_absent: bool | None = None
    retirement_tags_unchanged: bool | None = None
    retirement_result: str | None = None
    retirement_receipt_id: str | None = None
    fixture_exceptions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    reviewed_sensitive_sources: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    diagnostics_requested: bool = False
    entrypoint: Path | None = None
    scan_diagnostic: dict[str, Any] | None = None
    trailers_applied: list[str] = dataclasses.field(default_factory=list)
    agent_trace_note: dict[str, Any] | None = None
    external_scanners: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    policy_verdict: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the canonical `--summary` JSON envelope preserving exact fixed keys & ordering."""
        status = self.status if self.status in ALLOWED_STATUSES else "failed"
        next_action = (
            self.next_action if self.next_action in ALLOWED_NEXT_ACTIONS else "inspect_failure"
        )
        warnings, dropped_warnings, warning_clipped = _parse_warnings(self.warnings)
        warnings_omitted = dropped_warnings + max(0, self.warnings_omitted_extra)
        reason_text, reason_clipped = _limit_text(self.reason, 400)
        next_action_text, next_action_clipped = _limit_text(next_action, 120)
        if next_action_text is None:
            next_action_text = "inspect_failure"

        commit_obj: dict[str, Any] = {
            "created": bool(self.commit_created),
            "sha": self.commit_sha or None,
        }
        push_obj: dict[str, Any] = {
            "executed": bool(self.push_executed),
            "result": self.push_result or "not_run",
            "branch_refspec_only": self.push_branch_refspec_only,
            "follow_tags_requested": self.push_follow_tags_requested,
        }
        mode_result = _build_mode_result(self)

        payload: dict[str, Any] = {
            "summary_schema_version": SUMMARY_SCHEMA_VERSION,
            "finalizer_version": __version__,
            "mode": self.mode or "normal",
            "status": status,
            "final_phase": self.final_phase or "preflight",
            "exit_code": int(self.exit_code),
            "repository": self.repository or None,
            "repository_id": self.repository_id or None,
            "allocation_id": self.allocation_id or None,
            "task_key": self.task_key or None,
            "authority_key": self.authority_key or None,
            "worktree_path": self.worktree_path or None,
            "role": self.role or None,
            "branch": self.branch or None,
            "upstream": self.upstream or None,
            "requested_path_count": int(self.requested_path_count),
            "dry_run": bool(self.dry_run),
            "commit": commit_obj,
            "push": push_obj,
            "warnings": warnings,
            "warnings_omitted": warnings_omitted,
            "warning_characters_omitted": warning_clipped,
            "reason": reason_text,
            "reason_omitted_characters": reason_clipped,
            "next_action": next_action_text,
            "next_action_omitted_characters": next_action_clipped,
            "mode_result": mode_result,
        }
        if status != "success":
            payload["error_code"] = self.error_code or (
                ErrorCode.INTERNAL_ERROR if status == "failed" else ErrorCode.CLI_INVALID_ARGS
            )
        if self.resume_required:
            payload["resume"] = {
                "head_oid": self.resume_oid,
                "remote": self.resume_remote,
            }
        if self.fixture_exceptions:
            payload["fixture_exceptions"] = list(self.fixture_exceptions)
        if self.reviewed_sensitive_sources:
            reviews = [dict(item) for item in self.reviewed_sensitive_sources]
            if status == "success" and not self.dry_run:
                for rev in reviews:
                    rev["content_scan"] = "passed"
            payload["reviewed_sensitive_sources"] = reviews
        if self.diagnostics_requested or self.scan_diagnostic is not None:
            rt = runtime_identity(self.entrypoint)
            if self.scan_diagnostic and self.scan_diagnostic.get("scanner_sha256") and "content_scanner" in rt:
                rt["content_scanner"]["sha256"] = str(self.scan_diagnostic["scanner_sha256"])
            content_scan_out = None
            if self.scan_diagnostic is not None:
                content_scan_out = {
                    k: self.scan_diagnostic.get(k)
                    for k in ("path", "stage", "verdict", "detector", "byte_length", "binary", "sha256", "blob_oid")
                }
            diag_obj: dict[str, Any] = {
                "schema_version": 1,
                "runtime": rt,
                "content_scan": content_scan_out,
            }
            if self.scan_diagnostic and "external_scanners" in self.scan_diagnostic:
                diag_obj["external_scanners"] = self.scan_diagnostic["external_scanners"]
            payload["diagnostics"] = diag_obj
        return payload


def _build_mode_result(state: SummaryState) -> dict[str, Any]:
    mode = state.mode or "normal"
    if mode == "initial":
        mr: dict[str, Any] = {
            "initial_publish": state.mode_state,
            "snapshot_verification": state.snapshot_verification,
            "remote_conclusion": state.remote_conclusion,
            "post_verify": state.post_verify,
        }
    elif mode == "initial_branch":
        mr = {
            "initial_branch_publish": state.mode_state,
            "remote_conclusion": state.remote_conclusion,
            "post_verify": state.post_verify,
        }
    elif mode in {"publish_existing_branch", "publish_existing_history"}:
        interface = (
            "publish-existing-history"
            if mode == "publish_existing_history"
            else "publish-existing-branch"
        )
        mr = {
            "interface": interface,
            "commit_reused": state.commit_reused,
            "remote_conclusion": state.remote_conclusion,
            "pre_publish": {
                "commit_count": state.publish_commit_count,
                "commit_range": state.publish_range or None,
            },
            "push_target": state.push_target or None,
            "post_verify": state.post_verify,
            "head": {
                "before": state.head_before or None,
                "after": state.head_after or None,
                "unchanged": state.head_unchanged,
            },
            "index_unchanged": state.index_unchanged,
            "worktree": {
                "before": state.worktree_before_state or None,
                "after": state.worktree_state or None,
                "unchanged": state.worktree_unchanged,
            },
            "final": {
                "head": state.final_head or None,
                "remote_ref_oid": state.final_remote_oid or None,
            },
        }
        if mode == "publish_existing_history":
            mr["bootstrap_prerequisite"] = "repo-plan/repo-ensure"
    elif mode == "sync_published_branch":
        mr = {
            "interface": "sync-published-branch",
            "source_remote": state.sync_source_remote or None,
            "published_oid": state.commit_sha or None,
            "expected_target_oid": state.sync_expected_target_oid or None,
            "push_target": state.push_target or None,
            "commit_count": state.publish_commit_count,
            "post_verify": state.post_verify,
            "remote_conclusion": state.remote_conclusion,
            "final_remote_oid": state.final_remote_oid or None,
            "local_head_unchanged": state.head_unchanged,
            "local_upstream_unchanged": bool(
                state.head_unchanged and state.index_unchanged and state.worktree_unchanged
            ),
        }
    elif mode == "resume":
        resume_interface = {
            "initial_root": "resume-initial-publish",
            "existing_commits": "resume-publish",
            "existing_history_first": "resume-existing-history-publish",
        }.get(state.resume_publish_kind or "")
        mr = {
            "interface": resume_interface,
            "publish_kind": state.resume_publish_kind or None,
            "recovery_start": state.recovery_start or None,
            "commit_reused": state.commit_reused,
            "completed_through": state.completed_through,
            "resume_required": state.resume_required,
            "pre_publish": {
                "ahead": state.publish_ahead,
                "behind": state.publish_behind,
                "commit_count": state.publish_commit_count,
                "commit_range": state.publish_range or None,
            },
            "push_target": state.push_target or None,
            "post_verify": state.post_verify,
            "final": {
                "head": state.final_head or None,
                "remote_ref_oid": state.final_remote_oid or None,
                "worktree": state.worktree_state or None,
            },
        }
    elif mode == "initial-commit-only":
        mr = {
            "initial_root_commit": state.mode_state,
            "remote_verification": state.remote_conclusion,
            "post_verify": state.post_verify,
            "worktree": state.worktree_state or None,
        }
    elif mode == "commit-only":
        mr = {
            "remote_verification": state.remote_conclusion,
            "post_verify": state.post_verify,
            "worktree": state.worktree_state or None,
        }
    elif mode == "verify-only":
        mr = {
            "local_validation": state.mode_state,
            "commit": state.commit_result,
            "remote_verification": state.remote_conclusion,
            "post_verify": state.post_verify,
            "head": {
                "before": state.head_before or None,
                "after": state.head_after or None,
                "unchanged": state.head_unchanged,
            },
            "index_unchanged": state.index_unchanged,
            "worktree": {
                "before": state.worktree_before_state or None,
                "after": state.worktree_state or None,
                "unchanged": state.worktree_unchanged,
            },
        }
    elif mode in {"retire_local_branch", "retire_remote_branch"}:
        operation_name = "local" if mode == "retire_local_branch" else "remote"
        mr = {
            "interface": f"retire-{operation_name}-branch",
            "operation": state.retirement_operation or None,
            "remote": state.retirement_remote or None,
            "branch": state.retirement_branch or None,
            f"{operation_name}_ref": state.retirement_ref or None,
            f"expected_{operation_name}_oid": state.retirement_expected_oid or None,
            "expected_oid": state.retirement_expected_oid or None,
            "retirement_plan_id": state.retirement_plan_id or None,
            "integrated_into_ref": state.retirement_integrated_ref or None,
            "integrated_into_oid": state.retirement_integrated_oid or None,
            "ancestry_verified": state.retirement_ancestry_verified,
            "ci_required": state.retirement_ci_required,
            "ci_status": state.retirement_ci_status,
            "ci_commit_oid": state.retirement_ci_commit_oid or None,
            "ci_verification_source": state.retirement_ci_source,
            "ci_verified_at": state.retirement_ci_verified_at or None,
            "expected_oid_lease_bound": state.retirement_lease_bound,
            "remote_absent_after": state.retirement_remote_absent_after,
            "local_absent_after": state.retirement_local_absent_after,
            "remote_tracking_ref_absent": state.retirement_remote_tracking_absent,
            "head": {
                "before": state.head_before or None,
                "after": state.head_after or None,
                "unchanged": state.head_unchanged,
            },
            "index_unchanged": state.index_unchanged,
            "worktree": {
                "clean_before": state.worktree_before_state == "clean",
                "clean_after": state.worktree_state == "clean",
                "unchanged": state.worktree_unchanged,
            },
            "tags_unchanged": state.retirement_tags_unchanged,
            "result": state.retirement_result,
        }
        if state.retirement_receipt_id:
            mr["retirement_receipt_id"] = state.retirement_receipt_id
    else:
        mr = {"post_verify": state.post_verify}

    if state.trailers_applied:
        mr["trailers_applied"] = list(state.trailers_applied)
    if state.agent_trace_note is not None:
        mr["agent_trace"] = dict(state.agent_trace_note)
    if state.external_scanners:
        mr["external_scanners"] = list(state.external_scanners)
    if state.policy_verdict is not None:
        mr["policy"] = dict(state.policy_verdict)
    return mr


def render_fallback_error_summary(reason: str, error_code: str = ErrorCode.SUMMARY_SERIALIZATION_ERROR) -> str:
    return json.dumps(
        {
            "summary_schema_version": SUMMARY_SCHEMA_VERSION,
            "summary_status": "serialization_failed",
            "operation_status": "failed",
            "operation_exit_code": 1,
            "error_code": error_code,
            "reason": reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@contextmanager
def execution_lock(entrypoint: Path) -> Iterator[None]:
    """Hold LOCK_SH with O_CLOEXEC when an execution lockfile exists alongside the entrypoint.

    Child processes (git, ssh, hooks, companion tools) never inherit the lock FD.
    """
    resolved = entrypoint.resolve()
    lock_path = entrypoint.with_name(EXECUTION_LOCK_NAME)
    if not lock_path.exists() and not lock_path.is_symlink():
        alt = resolved.with_name(EXECUTION_LOCK_NAME)
        if alt.exists() or alt.is_symlink():
            lock_path = alt
        else:
            yield
            return
    if lock_path.is_symlink():
        raise FinalizerError(
            ErrorCode.EXECUTION_LOCK_UNAVAILABLE,
            "拒绝符号链接形式的 Finalizer 执行锁",
            final_phase="cli",
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(lock_path, flags)
    except OSError as exc:
        raise FinalizerError(
            ErrorCode.EXECUTION_LOCK_UNAVAILABLE,
            f"无法打开 Finalizer 执行锁: {exc}",
            final_phase="cli",
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (info.st_mode & 0o077):
            raise FinalizerError(
                ErrorCode.EXECUTION_LOCK_UNAVAILABLE,
                "Finalizer 执行锁权限或所有权不安全",
                final_phase="cli",
            )
        deadline = time.monotonic() + 30.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise FinalizerError(
                        ErrorCode.EXECUTION_LOCK_TIMEOUT,
                        "等待 Finalizer 执行锁超时",
                        status="failed",
                        final_phase="cli",
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)
