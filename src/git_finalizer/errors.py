"""Structured error codes and exception taxonomy across Git Finalizer CLIs."""

from __future__ import annotations

import re

VALID_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class ErrorCode:
    """Stable machine-readable error codes emitted on refusal or failure."""

    CLI_INVALID_ARGS = "CLI_INVALID_ARGS"
    EXECUTION_LOCK_UNAVAILABLE = "EXECUTION_LOCK_UNAVAILABLE"
    EXECUTION_LOCK_TIMEOUT = "EXECUTION_LOCK_TIMEOUT"
    RUNTIME_INTEGRITY_ERROR = "RUNTIME_INTEGRITY_ERROR"
    REPO_INVALID = "REPO_INVALID"
    BRANCH_PROTECTED = "BRANCH_PROTECTED"
    DETACHED_HEAD = "DETACHED_HEAD"
    UNBORN_REPO = "UNBORN_REPO"
    UPSTREAM_CONFLICT = "UPSTREAM_CONFLICT"
    REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
    REMOTE_DIVERGED = "REMOTE_DIVERGED"
    REMOTE_BEHIND = "REMOTE_BEHIND"
    REMOTE_MISSING = "REMOTE_MISSING"
    SCOPE_INVALID = "SCOPE_INVALID"
    INDEX_UNCLEAN = "INDEX_UNCLEAN"
    WORKING_TREE_DIRTY = "WORKING_TREE_DIRTY"
    WHITESPACE_VIOLATION = "WHITESPACE_VIOLATION"
    BINARY_SIZE_EXCEEDED = "BINARY_SIZE_EXCEEDED"
    CONTENT_SCAN_BLOCKED = "CONTENT_SCAN_BLOCKED"
    EXTERNAL_SCANNER_FAILED = "EXTERNAL_SCANNER_FAILED"
    EXTERNAL_SCANNER_BLOCKED = "EXTERNAL_SCANNER_BLOCKED"
    SOURCE_REVIEW_BLOCKED = "SOURCE_REVIEW_BLOCKED"
    FIXTURE_EXCEPTION_BLOCKED = "FIXTURE_EXCEPTION_BLOCKED"
    SNAPSHOT_EVIDENCE_REJECTED = "SNAPSHOT_EVIDENCE_REJECTED"
    POLICY_DENIED = "POLICY_DENIED"
    HOOK_FAILED = "HOOK_FAILED"
    COMMIT_FAILED = "COMMIT_FAILED"
    PUSH_FAILED = "PUSH_FAILED"
    POST_VERIFY_FAILED = "POST_VERIFY_FAILED"
    RETIREMENT_BLOCKED = "RETIREMENT_BLOCKED"
    CONTROLLER_UNAVAILABLE = "CONTROLLER_UNAVAILABLE"
    BOOTSTRAP_BLOCKED = "BOOTSTRAP_BLOCKED"
    BOOTSTRAP_FAILED = "BOOTSTRAP_FAILED"
    INTEGRATION_PUBLISH_BLOCKED = "INTEGRATION_PUBLISH_BLOCKED"
    INTEGRATION_PUBLISH_FAILED = "INTEGRATION_PUBLISH_FAILED"
    SYNC_FAILED = "SYNC_FAILED"
    TEMP_DIR_FAILED = "TEMP_DIR_FAILED"
    SUMMARY_SERIALIZATION_ERROR = "SUMMARY_SERIALIZATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class FinalizerError(RuntimeError):
    """Fail-closed operational blocker or terminal failure with a structured code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "blocked",
        final_phase: str = "preflight",
        next_action: str | None = None,
        exit_code: int = 1,
        commit_created: bool = False,
        commit_sha: str | None = None,
        push_executed: bool = False,
        push_result: str = "not_run",
        mode_result: dict[str, object] | None = None,
        resume: dict[str, object] | None = None,
        diagnostics: dict[str, object] | None = None,
        scan_diagnostic: dict[str, object] | None = None,
        reviewed_sensitive_sources: list[dict[str, object]] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        if VALID_ERROR_CODE.fullmatch(code) is None:
            raise ValueError(f"invalid structured error_code: {code!r}")
        if status not in {"blocked", "failed"}:
            raise ValueError(f"invalid error status: {status!r}")
        self.code = code
        self.message = message
        self.status = status
        self.final_phase = final_phase
        self.next_action = (
            next_action
            if next_action is not None
            else ("inspect_failure" if status == "failed" else "resolve_blocker_and_retry")
        )
        self.exit_code = exit_code
        self.commit_created = commit_created
        self.commit_sha = commit_sha
        self.push_executed = push_executed
        self.push_result = push_result
        self.mode_result = dict(mode_result) if mode_result is not None else {}
        self.resume = dict(resume) if resume is not None else None
        self.diagnostics = dict(diagnostics) if diagnostics is not None else None
        self.scan_diagnostic = dict(scan_diagnostic) if scan_diagnostic is not None else None
        self.reviewed_sensitive_sources = (
            [dict(item) for item in reviewed_sensitive_sources]
            if reviewed_sensitive_sources is not None
            else None
        )
        self.warnings = list(warnings or [])
        super().__init__(message)
