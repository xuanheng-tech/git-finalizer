"""Reviewed-sensitive-source validator for Git Finalizer."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from git_finalizer.delivery.content_scan import MAX_SCAN_BYTES, scan
from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import FULL_OID_RE, clean_git_env, read_baseline_blob, run_git
from git_finalizer.json_io import loads_unique, sha256_bytes

MAX_REVIEW_BYTES = 64 * 1024
MAX_REVIEW_ITEMS = 64
MAX_VALIDITY = timedelta(days=7)
_ENV_RE = re.compile(r"(?:^|/)\.env(?:$|[._-])", re.IGNORECASE)
_MARKER_RE = re.compile(r"(?:^|[/._ -])(?:credentials?|secrets?|tokens?)(?:[/._ -]|$)", re.IGNORECASE)
_KEY_RE = re.compile(r"(?:^|[/._ -])(?:private|ssh)[._ -]?key(?:[/._ -]|$)", re.IGNORECASE)
_SSH_NAME_RE = re.compile(r"(?:^|/)(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:\.pub)?$", re.IGNORECASE)
_KEY_EXT_RE = re.compile(r"\.(?:pem|key|p12|pfx)$", re.IGNORECASE)


class ReviewError(FinalizerError):
    """Static, non-leaking sensitive-source review rejection."""

    def __init__(self, review_code: str, static_reason: str) -> None:
        self.review_code = review_code
        self.static_reason = static_reason
        super().__init__(
            ErrorCode.SOURCE_REVIEW_BLOCKED,
            f"reviewed-sensitive-source validation blocked ({review_code}): {static_reason}",
            final_phase="local_validation",
        )


def sensitive_path_reason(path: str) -> str | None:
    """Classify sensitive path reason matching exact path markers."""
    lower = path.lower()
    if _ENV_RE.search(lower):
        return ".env 路径"
    if _KEY_RE.search(lower) or _SSH_NAME_RE.search(lower) or _KEY_EXT_RE.search(lower):
        return "private key/SSH key 路径标记"
    if _MARKER_RE.search(lower):
        return "credential/secret/token 路径标记"
    return None


def is_sensitive_path(path: str) -> bool:
    return sensitive_path_reason(path) is not None


def _is_disallowed_source_path(path: str) -> bool:
    return sensitive_path_reason(path) != "credential/secret/token 路径标记"


def read_external_regular_bounded(
    path_str: str,
    *,
    repo: Path,
    common_dir: Path,
    label: str,
) -> tuple[bytes, Path]:
    """Open and verify an external non-symlink regular file without stat->read TOCTOU.

    Accepts owner-writable UPG modes like 0664 (`st_mode & 0o002 == 0`) while blocking world-writable (`o+w`).
    """
    raw_path = Path(path_str)
    if not raw_path.is_absolute():
        raise ReviewError("REVIEW_PATH_NOT_ABSOLUTE", f"{label} path must be absolute")
    resolved_parent = raw_path.parent.resolve()
    resolved = resolved_parent / raw_path.name
    repo_resolved = repo.resolve()
    common_resolved = common_dir.resolve()
    if resolved == repo_resolved or resolved.is_relative_to(repo_resolved):
        raise ReviewError("REVIEW_IN_REPOSITORY", f"{label} must reside outside the repository")
    if resolved == common_resolved or resolved.is_relative_to(common_resolved):
        raise ReviewError("REVIEW_IN_GIT_DIR", f"{label} must reside outside .git")

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(raw_path, flags)
    except OSError as exc:
        raise ReviewError("REVIEW_FILE_UNREADABLE", f"{label} cannot be opened safely: {exc.strerror or 'os error'}") from exc

    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise ReviewError("REVIEW_STAT_FAILED", f"{label} metadata unavailable") from exc

    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ReviewError("REVIEW_NOT_REGULAR", f"{label} must be a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        os.close(fd)
        raise ReviewError("REVIEW_OWNER_MISMATCH", f"{label} must be owned by the caller")
    if info.st_mode & 0o002:
        os.close(fd)
        raise ReviewError("REVIEW_WORLD_WRITABLE", f"{label} must not be world-writable")
    if info.st_size > MAX_REVIEW_BYTES:
        os.close(fd)
        raise ReviewError("REVIEW_SIZE_EXCEEDED", f"{label} exceeds 64 KiB limit")

    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_REVIEW_BYTES + 1)
    if len(data) > MAX_REVIEW_BYTES:
        raise ReviewError("REVIEW_SIZE_EXCEEDED", f"{label} exceeds 64 KiB limit")
    return data, resolved


def _parse_iso_utc(raw: Any, label: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ReviewError("REVIEW_TIMESTAMP_INVALID", f"{label} timestamp missing or invalid")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ReviewError("REVIEW_TIMESTAMP_INVALID", f"{label} timestamp invalid") from exc
    if parsed.tzinfo is None:
        raise ReviewError("REVIEW_TIMESTAMP_NAIVE", f"{label} timestamp must include timezone offset")
    return parsed.astimezone(UTC)


def _validate_source_syntax(path: str, content: bytes, allowed_extensions: tuple[str, ...]) -> None:
    if _is_disallowed_source_path(path):
        raise ReviewError("REVIEW_PATH_FORBIDDEN", "disallowed secret file pattern")
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in allowed_extensions:
        raise ReviewError("REVIEW_EXTENSION_UNSUPPORTED", f"unsupported extension for reviewed source: {suffix or '(none)'}")
    if len(content) > 1024 * 1024:
        raise ReviewError("REVIEW_SOURCE_TOO_LARGE", "reviewed source exceeds 1 MiB limit")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewError("REVIEW_INVALID_UTF8", "reviewed source must be valid UTF-8") from exc
    if "\x00" in text:
        raise ReviewError("REVIEW_INVALID_NUL", "reviewed source must not contain NUL bytes")
    if suffix == ".py":
        try:
            ast.parse(text)
        except SyntaxError as exc:
            raise ReviewError("REVIEW_INVALID_PYTHON", "reviewed Python source contains syntax errors") from exc
    elif suffix == ".json":
        try:
            loads_unique(text)
        except ValueError as exc:
            raise ReviewError("REVIEW_INVALID_JSON", f"reviewed JSON source invalid: {exc}") from exc


def _git_output(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    completed = run_git(repo, *arguments, env=clean_git_env(env))
    if completed.returncode != 0:
        raise ReviewError("REVIEW_GIT_COMMAND_FAILED", f"git {arguments[0]} failed")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _commit_root(repo: Path, oid: str, env: dict[str, str] | None = None) -> str:
    roots = _git_output(repo, "rev-list", "--max-parents=0", oid, env=env).splitlines()
    if len(roots) != 1 or FULL_OID_RE.fullmatch(roots[0]) is None:
        raise ReviewError("REVIEW_ROOT_AMBIGUOUS", "repository root commit is missing or ambiguous")
    return roots[0]


def _head_oid(repo: Path, env: dict[str, str] | None = None) -> str:
    completed = run_git(repo, "rev-parse", "--verify", "HEAD", env=clean_git_env(env))
    if completed.returncode != 0:
        raise ReviewError("REVIEW_UNBORN_REPO", "reviewed-sensitive-source requires an existing HEAD commit")
    return completed.stdout.decode("ascii").strip()


def validate_review_document(
    *,
    review_file: str,
    repo: Path,
    common_dir: Path,
    repository_id: str | None,
    allocation_id: str | None,
    task_key: str | None,
    authority_key: str | None,
    worktree_path: str | None,
    requested_paths: list[str],
    stage: str = "worktree",
    stage_env: dict[str, str] | None = None,
    allowed_extensions: tuple[str, ...] = (".py", ".css", ".json"),
    candidate_blobs: dict[str, bytes] | None = None,
    commit_ref: str | None = None,
    baseline_ref: str | None = "HEAD",
) -> list[dict[str, Any]]:
    """Validate an external reviewed-sensitive-source JSON against linkage, scope, expiry, and source bytes."""
    if not all([repository_id, allocation_id, task_key, authority_key]):
        raise FinalizerError(
            ErrorCode.SOURCE_REVIEW_BLOCKED,
            "source review 要求完整 Controller linkage（--repository-id/--allocation-id/--task-key/--authority-key）",
            final_phase="cli",
        )
    raw_review, resolved_review_file = read_external_regular_bounded(
        review_file, repo=repo, common_dir=common_dir, label="review"
    )
    try:
        doc = loads_unique(raw_review)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReviewError("REVIEW_JSON_INVALID", f"invalid review JSON: {exc}") from exc

    if not isinstance(doc, dict) or doc.get("schema_version") != 1 or doc.get("kind") != "reviewed-sensitive-source":
        raise ReviewError("REVIEW_SCHEMA_INVALID", "review schema_version or kind mismatch")

    repo_meta = doc.get("repository")
    alloc_meta = doc.get("allocation")
    if not isinstance(repo_meta, dict) or not isinstance(alloc_meta, dict):
        raise ReviewError("REVIEW_METADATA_MISSING", "repository or allocation metadata missing")

    expected_common = str(common_dir.resolve())
    if repo_meta.get("common_dir") != expected_common:
        raise ReviewError("REVIEW_COMMON_DIR_MISMATCH", "repository common_dir mismatch")
    head_oid = _head_oid(repo, env=stage_env)
    expected_root = _commit_root(repo, head_oid, env=stage_env)
    if repo_meta.get("root_commit") != expected_root:
        raise ReviewError("REVIEW_ROOT_COMMIT_MISMATCH", "repository root_commit mismatch")

    expected_wt = str(Path(worktree_path or repo).resolve())
    if (
        alloc_meta.get("repository_id") != repository_id
        or alloc_meta.get("allocation_id") != allocation_id
        or alloc_meta.get("task_key") != task_key
        or alloc_meta.get("authority_key") != authority_key
        or alloc_meta.get("worktree_path") != expected_wt
    ):
        raise ReviewError("REVIEW_ALLOCATION_MISMATCH", "Controller allocation linkage mismatch")

    reviewed_at = _parse_iso_utc(doc.get("reviewed_at"), "reviewed_at")
    expires_at = _parse_iso_utc(doc.get("expires_at"), "expires_at")
    now = datetime.now(UTC)
    if expires_at <= reviewed_at or (expires_at - reviewed_at) > MAX_VALIDITY:
        raise ReviewError("REVIEW_EXPIRY_WINDOW_INVALID", "review expiry window exceeds 7 days or is inverted")
    if now >= expires_at:
        raise ReviewError("REVIEW_EXPIRED", "review has expired")

    expected_scope = "\n".join(sorted(requested_paths)) + "\n"
    if doc.get("scope_sha256") != sha256_bytes(expected_scope.encode("utf-8")):
        raise ReviewError("REVIEW_SCOPE_MISMATCH", "scope_sha256 does not match requested paths")

    reviews = doc.get("reviews")
    if not isinstance(reviews, list) or not reviews or len(reviews) > MAX_REVIEW_ITEMS:
        raise ReviewError("REVIEW_LIST_INVALID", "reviews list empty or oversized")

    seen_paths: set[str] = set()
    receipts: list[dict[str, Any]] = []
    requested_set = set(requested_paths)

    for item in reviews:
        if not isinstance(item, dict):
            raise ReviewError("REVIEW_ITEM_INVALID", "review entry must be an object")
        path = item.get("path")
        expected_sha = item.get("sha256")
        purpose = item.get("purpose")
        evidence = item.get("evidence")
        if (
            not isinstance(path, str)
            or not path
            or path in seen_paths
            or (bool(requested_set) and path not in requested_set)
            or _is_disallowed_source_path(path)
            or PurePosixPath(path).is_absolute()
            or str(PurePosixPath(path)) != path
            or ".." in PurePosixPath(path).parts
            or any(c in path for c in "*?[]\\\x00\n\r\t")
            or PurePosixPath(path).suffix.lower() not in allowed_extensions
            or not isinstance(expected_sha, str)
            or not isinstance(purpose, str)
            or not purpose.strip()
            or not isinstance(evidence, dict)
        ):
            raise ReviewError("REVIEW_ITEM_FIELDS_INVALID", "invalid path, duplicate path, or missing purpose")
        seen_paths.add(path)

        ev_path = evidence.get("path")
        ev_sha = evidence.get("sha256")
        if not isinstance(ev_path, str) or not isinstance(ev_sha, str):
            raise ReviewError("REVIEW_EVIDENCE_INVALID", "evidence metadata missing")
        ev_bytes, _ = read_external_regular_bounded(
            ev_path, repo=repo, common_dir=common_dir, label="evidence"
        )
        if sha256_bytes(ev_bytes) != ev_sha:
            raise ReviewError("REVIEW_EVIDENCE_HASH_MISMATCH", "evidence sha256 mismatch")

        if candidate_blobs and path in candidate_blobs:
            actual_bytes = candidate_blobs[path]
        elif stage == "head" and commit_ref:
            show = run_git(repo, "--literal-pathspecs", "cat-file", "blob", f"{commit_ref}:{path}", env=clean_git_env(stage_env))
            if show.returncode != 0:
                raise ReviewError("REVIEW_HEAD_BLOB_MISSING", "reviewed file missing in created commit")
            actual_bytes = show.stdout
        elif stage == "index":
            show = run_git(repo, "--literal-pathspecs", "cat-file", "blob", f":{path}", env=clean_git_env(stage_env))
            if show.returncode != 0:
                raise ReviewError("REVIEW_INDEX_BLOB_MISSING", "reviewed file missing in index")
            actual_bytes = show.stdout
        else:
            target = repo / path
            if target.is_symlink() or not target.is_file():
                raise ReviewError("REVIEW_TARGET_NOT_REGULAR", "reviewed target is not a regular file")
            actual_bytes = target.read_bytes()

        _validate_source_syntax(path, actual_bytes, allowed_extensions)
        if sha256_bytes(actual_bytes) != expected_sha:
            raise ReviewError("REVIEW_SOURCE_HASH_MISMATCH", "reviewed file sha256 mismatch")

        baseline_bytes: bytes | None = None
        if baseline_ref is not None:
            _, baseline_bytes = read_baseline_blob(
                repo, baseline_ref, path, max_bytes=MAX_SCAN_BYTES, env=stage_env
            )
        verdict = scan(actual_bytes, baseline=baseline_bytes)
        content_status = "passed" if verdict == "CLEAR" else "blocked"
        receipts.append(
            {
                "review_id": f"sha256:{sha256_bytes(raw_review)}",
                "path": path,
                "sha256": expected_sha,
                "purpose_sha256": sha256_bytes(purpose.encode("utf-8")),
                "evidence_sha256": ev_sha,
                "repository": repo_meta,
                "allocation": alloc_meta,
                "scope_sha256": doc["scope_sha256"],
                "path_review": "validated",
                "content_scan": content_status,
            }
        )
        if content_status != "passed":
            raise FinalizerError(
                ErrorCode.CONTENT_SCAN_BLOCKED,
                f"reviewed-sensitive-source 内容扫描命中敏感特征 ({verdict}): {path}",
                final_phase="local_validation",
                reviewed_sensitive_sources=receipts,
            )

    missing_sensitive = [p for p in requested_paths if is_sensitive_path(p) and p not in seen_paths]
    if missing_sensitive:
        raise ReviewError("REVIEW_INCOMPLETE", "not all sensitive paths are covered by review")

    return receipts


def reviewed_blob_map(
    repo: Path,
    receipts: list[dict[str, Any]],
    *,
    commit_ref: str = "HEAD",
) -> dict[str, str]:
    """Resolve `path -> blob_oid` on `commit_ref` for all reviewed paths in `receipts`."""
    mapping: dict[str, str] = {}
    for rec in receipts:
        rel = str(rec["path"])
        res = run_git(repo, "--literal-pathspecs", "ls-tree", "-z", commit_ref, "--", rel)
        if res.returncode != 0 or not res.stdout:
            raise FinalizerError(
                ErrorCode.SOURCE_REVIEW_BLOCKED,
                f"无法解析 reviewed source 对象: {rel}",
                final_phase="local_validation",
            )
        meta, _, _ = res.stdout.rstrip(b"\0").decode("utf-8", errors="strict").partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            raise FinalizerError(
                ErrorCode.SOURCE_REVIEW_BLOCKED,
                f"reviewed source 对象类型异常: {rel}",
                final_phase="local_validation",
            )
        mapping[rel] = parts[2]
    return mapping
