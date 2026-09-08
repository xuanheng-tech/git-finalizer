"""Local control-plane metadata for completed but unpublished repository tasks."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
MIGRATION_SOURCE_SCHEMA = "completed-unpublished-migration-source/v1"
MIGRATION_RESULT_SCHEMA = "completed-unpublished-migration-dry-run/v1"
MAX_RECORD_BYTES = 8 * 1024
MAX_SCOPE_PATHS = 16
MAX_WORKSTREAM_CHARS = 96
ACTIVE_STATES = frozenset({"pending", "blocked"})
TERMINAL_STATES = frozenset({"published", "superseded", "dismissed"})
QUEUE_STATES = ACTIVE_STATES | TERMINAL_STATES
PUBLICATION_DECISIONS = frozenset(
    {"not_applicable", "publication_blocked", "intentionally_unpublished", "publish_now"}
)
FINALIZATION_SCOPES = frozenset(
    {"not_applicable", "commit_only", "commit_and_push"}
)
FINALIZATION_OUTCOMES = frozenset(
    {"not_run", "local_commit_created", "remote_pushed", "remote_verified", "blocked"}
)
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low", "unknown"})
REF_RE = re.compile(r"^(?:cuq|repo|task|thr|path|migration)_[0-9a-f]{32}$")
OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
RAW_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

RECORD_KEYS = frozenset(
    {
        "schema_version",
        "record_id",
        "repo_ref",
        "task_ref",
        "thread_ref",
        "workstream",
        "publication_decision",
        "finalization_scope",
        "finalization_outcome",
        "queue_state",
        "base_head",
        "path_scope",
        "created_at",
        "updated_at",
        "last_reviewed_at",
        "closure_evidence",
        "local_commit_oid",
    }
)


class QueueError(ValueError):
    """Raised when queue state or an operation fails a safety contract."""


@dataclass(frozen=True, slots=True)
class RepoFacts:
    root: Path
    repo_ref: str
    head: str | None
    upstream_ref: str | None
    upstream_oid: str | None
    ahead: int | None
    behind: int | None


def _default_state_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return root / "codex-exec" / "completed-unpublished" / "v1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validate_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        raise QueueError(f"{label} must be an RFC3339 timestamp")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QueueError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise QueueError(f"{label} must include a timezone")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_bytes(record: Mapping[str, object]) -> bytes:
    return _canonical_json(record) + b"\n"


def _digest_parts(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _pseudonym(key: bytes, domain: str, value: str, prefix: str) -> str:
    payload = f"{domain}\0{value}".encode("utf-8")
    return f"{prefix}_{hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]}"


def _owned_mode(path: Path, expected_mode: int, label: str) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise QueueError(f"{label} is not a regular file: {path}")
    if metadata.st_uid != os.getuid():
        raise QueueError(f"{label} is not owned by the current user: {path}")
    actual = stat.S_IMODE(metadata.st_mode)
    if actual != expected_mode:
        raise QueueError(
            f"{label} must have mode {expected_mode:#05o}, found {actual:#05o}: {path}"
        )


def _ensure_state_dir(state_dir: Path, *, create: bool) -> bool:
    if not state_dir.exists() and not state_dir.is_symlink():
        if not create:
            return False
        state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        state_dir.chmod(0o700)
    metadata = state_dir.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise QueueError(f"queue state path is not a regular directory: {state_dir}")
    if metadata.st_uid != os.getuid():
        raise QueueError(f"queue state directory is not owned by the current user: {state_dir}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        raise QueueError(f"queue state directory must have mode 0700, found {mode:#05o}")
    return True


def _load_key(state_dir: Path, *, create: bool) -> bytes | None:
    if not _ensure_state_dir(state_dir, create=create):
        return None
    path = state_dir / "key"
    if not path.exists() and not path.is_symlink():
        if not create:
            raise QueueError(f"queue key is missing: {path}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".key.", dir=state_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass
            directory_descriptor = os.open(state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    _owned_mode(path, 0o600, "queue key")
    key = path.read_bytes()
    if len(key) != 32:
        raise QueueError("queue key must contain exactly 32 bytes")
    return key


@contextmanager
def _queue_lock(state_dir: Path, *, create: bool, exclusive: bool) -> Iterator[bool]:
    if not _ensure_state_dir(state_dir, create=create):
        yield False
        return
    path = state_dir / "queue.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        _owned_mode(path, 0o600, "queue lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _git(
    repo: Path,
    *arguments: str,
    binary: bool = False,
    allow_failure: bool = False,
) -> str | bytes | None:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        if allow_failure:
            return None
        message = completed.stderr.decode("utf-8", errors="replace").strip()[:512]
        raise QueueError(f"git {' '.join(arguments)} failed: {message or completed.returncode}")
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _normalize_remote(value: str) -> str:
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme.lower(), host.lower() + port, parsed.path, "", ""))
    scp = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", value)
    if scp is not None:
        return f"ssh://{scp.group(1).lower()}/{scp.group(2).lstrip('/')}"
    path = Path(value).expanduser()
    return f"file://{path.resolve(strict=False)}"


def _repo_identity(root: Path) -> str:
    upstream = _git(
        root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        allow_failure=True,
    )
    remote_name: str | None = None
    if isinstance(upstream, str) and "/" in upstream:
        remote_name = upstream.split("/", 1)[0]
    if remote_name is None:
        remotes = _git(root, "remote", allow_failure=True)
        if isinstance(remotes, str):
            names = [item for item in remotes.splitlines() if item]
            if "origin" in names:
                remote_name = "origin"
            elif len(names) == 1:
                remote_name = names[0]
    if remote_name is not None:
        remote = _git(
            root,
            "config",
            "--get",
            f"remote.{remote_name}.url",
            allow_failure=True,
        )
        if isinstance(remote, str) and remote:
            return "remote\0" + _normalize_remote(remote)
    common = _git(root, "rev-parse", "--git-common-dir")
    assert isinstance(common, str)
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = root / common_path
    return "common-dir\0" + str(common_path.resolve(strict=False))


def inspect_repo(repo: Path, key: bytes) -> RepoFacts:
    if not repo.is_absolute():
        raise QueueError("--repo must be an absolute path")
    requested = repo.resolve(strict=True)
    top = _git(requested, "rev-parse", "--show-toplevel")
    assert isinstance(top, str)
    root = Path(top).resolve(strict=True)
    if requested != root:
        raise QueueError(f"--repo must be the canonical worktree root: {root}")
    head_value = _git(root, "rev-parse", "--verify", "HEAD", allow_failure=True)
    head = head_value if isinstance(head_value, str) and OID_RE.fullmatch(head_value) else None
    upstream_value = _git(
        root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        allow_failure=True,
    )
    upstream_ref = upstream_value if isinstance(upstream_value, str) and upstream_value else None
    upstream_oid_value = _git(
        root,
        "rev-parse",
        "--verify",
        "@{upstream}^{commit}",
        allow_failure=True,
    )
    upstream_oid = (
        upstream_oid_value
        if isinstance(upstream_oid_value, str) and OID_RE.fullmatch(upstream_oid_value)
        else None
    )
    ahead: int | None = None
    behind: int | None = None
    if head is not None and upstream_oid is not None:
        counts = _git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        assert isinstance(counts, str)
        fields = counts.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            raise QueueError("git returned an invalid ahead/behind result")
        ahead, behind = int(fields[0]), int(fields[1])
    repo_ref = _pseudonym(key, "unpublished-queue-repository", _repo_identity(root), "repo")
    return RepoFacts(root, repo_ref, head, upstream_ref, upstream_oid, ahead, behind)


def _normalize_workstream(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > MAX_WORKSTREAM_CHARS:
        raise QueueError(f"workstream must contain 1-{MAX_WORKSTREAM_CHARS} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise QueueError("workstream cannot contain control characters")
    return normalized


def _normalize_relative_path(value: str) -> str:
    if not value or "\x00" in value:
        raise QueueError("scope paths must be non-empty and cannot contain NUL")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise QueueError(f"scope path must be a normalized relative path: {value}")
    normalized = path.as_posix()
    if normalized != value:
        raise QueueError(f"scope path must be normalized: {value}")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _worktree_item(root: Path, relative_path: str, key: bytes) -> dict[str, object]:
    path = root / relative_path
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "relative_path": relative_path,
            "path_ref": _pseudonym(key, "unpublished-queue-path", relative_path, "path"),
            "state": "deleted",
            "mode": None,
            "sha256": None,
        }
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        state = "symlink"
        mode = "120000"
        digest = hashlib.sha256(target).hexdigest()
    elif stat.S_ISREG(metadata.st_mode):
        resolved_parent = path.parent.resolve(strict=True)
        if root not in (resolved_parent, *resolved_parent.parents):
            raise QueueError(f"scope path escapes the repository through a parent symlink: {relative_path}")
        state = "file"
        mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        digest = _sha256_file(path)
    else:
        raise QueueError(f"scope path is not a regular file, symlink, or deletion: {relative_path}")
    return {
        "relative_path": relative_path,
        "path_ref": _pseudonym(key, "unpublished-queue-path", relative_path, "path"),
        "state": state,
        "mode": mode,
        "sha256": digest,
    }


def build_path_scope(root: Path, paths: Sequence[str], key: bytes) -> dict[str, object]:
    normalized = sorted({_normalize_relative_path(value) for value in paths})
    if not normalized:
        raise QueueError("an active queue record requires at least one explicit scope path")
    path_set_sha256 = _digest_parts(normalized)
    if len(normalized) <= MAX_SCOPE_PATHS:
        items = [_worktree_item(root, value, key) for value in normalized]
        kind = "exact"
    else:
        items = [
            {
                "relative_path": value,
                "path_ref": _pseudonym(key, "unpublished-queue-path", value, "path"),
            }
            for value in normalized[:MAX_SCOPE_PATHS]
        ]
        kind = "bounded"
    unsigned = {
        "kind": kind,
        "path_count": len(normalized),
        "items": items,
        "paths_omitted": max(0, len(normalized) - len(items)),
        "path_set_sha256": path_set_sha256,
    }
    return {
        **unsigned,
        "scope_fingerprint": hashlib.sha256(_canonical_json(unsigned)).hexdigest(),
    }


def _desired_queue_state(decision: str, scope: str, outcome: str) -> str | None:
    if decision not in PUBLICATION_DECISIONS:
        raise QueueError(f"invalid publication decision: {decision}")
    if scope not in FINALIZATION_SCOPES:
        raise QueueError(f"invalid finalization scope: {scope}")
    if outcome not in FINALIZATION_OUTCOMES:
        raise QueueError(f"invalid finalization outcome: {outcome}")
    if decision == "not_applicable":
        if (scope, outcome) != ("not_applicable", "not_run"):
            raise QueueError("not_applicable requires not_applicable/not_run")
        return None
    if decision == "intentionally_unpublished":
        if (scope, outcome) != ("not_applicable", "not_run"):
            raise QueueError("intentionally_unpublished requires not_applicable/not_run")
        return "pending"
    if decision == "publication_blocked":
        if outcome != "blocked":
            raise QueueError("publication_blocked requires blocked outcome")
        return "blocked"
    if outcome == "remote_verified":
        if scope != "commit_and_push":
            raise QueueError("remote_verified requires commit_and_push")
        return None
    if outcome == "remote_pushed":
        if scope != "commit_and_push":
            raise QueueError("remote_pushed requires commit_and_push")
        return "blocked"
    if outcome == "blocked":
        if scope == "not_applicable":
            raise QueueError(f"publish_now/{outcome} requires a finalization scope")
        return "blocked"
    if outcome == "local_commit_created" and scope == "commit_only":
        return "pending"
    raise QueueError(
        f"unsupported task-end queue combination: decision={decision}, scope={scope}, outcome={outcome}"
    )


def _validate_path_scope(value: object) -> None:
    if not isinstance(value, dict):
        raise QueueError("path_scope must be an object")
    expected = {
        "kind",
        "path_count",
        "items",
        "paths_omitted",
        "path_set_sha256",
        "scope_fingerprint",
    }
    if set(value) != expected:
        raise QueueError("path_scope has unexpected fields")
    kind = value.get("kind")
    count = value.get("path_count")
    omitted = value.get("paths_omitted")
    items = value.get("items")
    if kind not in {"exact", "bounded"}:
        raise QueueError("path_scope.kind must be exact or bounded")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise QueueError("path_scope.path_count must be a positive integer")
    if not isinstance(omitted, int) or isinstance(omitted, bool) or omitted < 0:
        raise QueueError("path_scope.paths_omitted must be a non-negative integer")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SCOPE_PATHS:
        raise QueueError("path_scope.items must contain 1-16 items")
    if count != len(items) + omitted:
        raise QueueError("path_scope count and omitted count are inconsistent")
    if kind == "exact" and omitted != 0:
        raise QueueError("exact path scope cannot omit paths")
    if kind == "bounded" and omitted < 1:
        raise QueueError("bounded path scope must omit at least one path")
    paths: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise QueueError("path_scope item must be an object")
        required = {"relative_path", "path_ref"}
        if kind == "exact":
            required |= {"state", "mode", "sha256"}
        if set(item) != required:
            raise QueueError("path_scope item has unexpected fields")
        relative = item.get("relative_path")
        if not isinstance(relative, str):
            raise QueueError("path_scope relative_path must be a string")
        paths.append(_normalize_relative_path(relative))
        path_ref = item.get("path_ref")
        if not isinstance(path_ref, str) or REF_RE.fullmatch(path_ref) is None:
            raise QueueError("path_scope path_ref is invalid")
        if kind == "exact":
            if item.get("state") not in {"file", "symlink", "deleted"}:
                raise QueueError("exact path state is invalid")
            mode = item.get("mode")
            digest = item.get("sha256")
            if item.get("state") == "deleted":
                if mode is not None or digest is not None:
                    raise QueueError("deleted path cannot contain mode or sha256")
            elif mode not in {"100644", "100755", "120000"} or not isinstance(
                digest, str
            ) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise QueueError("exact path fingerprint is invalid")
    if paths != sorted(set(paths)):
        raise QueueError("path_scope items must be unique and sorted")
    for name in ("path_set_sha256", "scope_fingerprint"):
        digest = value.get(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise QueueError(f"path_scope.{name} is invalid")


def validate_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise QueueError("queue record must be an object")
    if set(record) != RECORD_KEYS:
        raise QueueError("queue record has unexpected fields")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise QueueError("queue record schema_version is unsupported")
    prefixes = {
        "record_id": "cuq_",
        "repo_ref": "repo_",
        "task_ref": "task_",
        "thread_ref": "thr_",
    }
    for name, prefix in prefixes.items():
        value = record.get(name)
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or REF_RE.fullmatch(value) is None
        ):
            raise QueueError(f"queue record {name} is invalid")
    workstream = record.get("workstream")
    if not isinstance(workstream, str) or _normalize_workstream(workstream) != workstream:
        raise QueueError("queue record workstream is invalid")
    decision = record.get("publication_decision")
    scope = record.get("finalization_scope")
    outcome = record.get("finalization_outcome")
    if not all(isinstance(value, str) for value in (decision, scope, outcome)):
        raise QueueError("queue record finalization fields must be strings")
    desired = _desired_queue_state(str(decision), str(scope), str(outcome))
    state = record.get("queue_state")
    if state not in QUEUE_STATES:
        raise QueueError("queue record state is invalid")
    if state in ACTIVE_STATES and desired != state:
        raise QueueError("active queue state does not match finalization facts")
    base_head = record.get("base_head")
    if base_head is not None and (
        not isinstance(base_head, str) or OID_RE.fullmatch(base_head) is None
    ):
        raise QueueError("queue record base_head is invalid")
    local_commit = record.get("local_commit_oid")
    if local_commit is not None and (
        not isinstance(local_commit, str) or OID_RE.fullmatch(local_commit) is None
    ):
        raise QueueError("queue record local_commit_oid is invalid")
    _validate_path_scope(record.get("path_scope"))
    _validate_timestamp(record.get("created_at"), "created_at")
    _validate_timestamp(record.get("updated_at"), "updated_at")
    reviewed = record.get("last_reviewed_at")
    if reviewed is not None:
        _validate_timestamp(reviewed, "last_reviewed_at")
    closure = record.get("closure_evidence")
    if state in ACTIVE_STATES:
        if closure is not None:
            raise QueueError("active queue record cannot contain closure_evidence")
    else:
        if not isinstance(closure, dict) or set(closure) != {"kind", "oid", "at"}:
            raise QueueError("terminal queue record requires closure_evidence")
        if not isinstance(closure.get("kind"), str) or not closure.get("kind"):
            raise QueueError("closure_evidence.kind is invalid")
        oid = closure.get("oid")
        if oid is not None and (not isinstance(oid, str) or OID_RE.fullmatch(oid) is None):
            raise QueueError("closure_evidence.oid is invalid")
        _validate_timestamp(closure.get("at"), "closure_evidence.at")
    encoded = _record_bytes(record)
    if len(encoded) > MAX_RECORD_BYTES:
        raise QueueError(
            f"queue record exceeds {MAX_RECORD_BYTES} bytes: {len(encoded)}"
        )
    return record


def _load_records_locked(state_dir: Path) -> dict[str, dict[str, object]]:
    path = state_dir / "queue.jsonl"
    if not path.exists() and not path.is_symlink():
        return {}
    _owned_mode(path, 0o600, "queue file")
    records: dict[str, dict[str, object]] = {}
    previous_id: str | None = None
    with path.open("rb") as stream:
        for line_no, raw in enumerate(stream, start=1):
            if not raw.endswith(b"\n"):
                raise QueueError(f"queue line {line_no} is not newline terminated")
            if len(raw) > MAX_RECORD_BYTES:
                raise QueueError(f"queue line {line_no} exceeds {MAX_RECORD_BYTES} bytes")
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise QueueError(f"queue line {line_no} is invalid JSON") from exc
            record = validate_record(value)
            record_id = str(record["record_id"])
            if record_id in records:
                raise QueueError(f"duplicate queue record_id: {record_id}")
            if previous_id is not None and record_id <= previous_id:
                raise QueueError("queue records must be strictly sorted by record_id")
            records[record_id] = record
            previous_id = record_id
    return records


def _write_records_locked(state_dir: Path, records: Mapping[str, Mapping[str, object]]) -> None:
    lines: list[bytes] = []
    for record_id in sorted(records):
        record = validate_record(dict(records[record_id]))
        if record_id != record["record_id"]:
            raise QueueError("queue record map key does not match record_id")
        lines.append(_record_bytes(record))
    _atomic_write(state_dir / "queue.jsonl", b"".join(lines))


def _identity_refs(key: bytes, task_id: str, thread_id: str) -> tuple[str, str]:
    if RAW_ID_RE.fullmatch(task_id) is None or RAW_ID_RE.fullmatch(thread_id) is None:
        raise QueueError("task_id and thread_id must be bounded structured identifiers")
    return (
        _pseudonym(key, "unpublished-queue-task", task_id, "task"),
        _pseudonym(key, "unpublished-queue-thread", thread_id, "thr"),
    )


def codex_rollouts(codex_home: Path, thread_id: str) -> list[Path]:
    """Return the Codex rollout files that may record this thread, newest name last."""
    if RAW_ID_RE.fullmatch(thread_id) is None:
        raise QueueError("thread_id must be a bounded structured identifier")
    sessions = codex_home / "sessions"
    return sorted(sessions.rglob(f"*{thread_id}*.jsonl"))


def resolve_current_task_id(codex_home: Path, thread_id: str) -> str:
    candidates = codex_rollouts(codex_home, thread_id)
    unmatched: list[str] = []
    for path in candidates:
        pending: dict[str, int] = {}
        observed_thread = False
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        continue
                    payload = value.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if value.get("type") == "session_meta" and payload.get("id") == thread_id:
                        observed_thread = True
                    if value.get("type") != "event_msg":
                        continue
                    turn = payload.get("turn_id")
                    if not isinstance(turn, str) or not turn:
                        continue
                    subtype = payload.get("type")
                    if subtype == "task_started":
                        pending[turn] = pending.get(turn, 0) + 1
                    elif subtype in {"task_complete", "turn_aborted"} and pending.get(turn, 0) > 0:
                        pending[turn] -= 1
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QueueError(f"cannot resolve current task lifecycle from {path.name}") from exc
        if observed_thread:
            unmatched.extend(turn for turn, count in pending.items() for _ in range(count))
    unique = sorted(set(unmatched))
    if len(unique) != 1:
        raise QueueError(
            f"expected exactly one unmatched task_started turn for current thread, found {len(unique)}"
        )
    return unique[0]


def upsert(
    *,
    state_dir: Path,
    repo: Path,
    task_id: str,
    thread_id: str,
    workstream: str,
    decision: str,
    scope: str,
    outcome: str,
    paths: Sequence[str],
    local_commit_oid: str | None = None,
    remote_verified_oid: str | None = None,
    now: str | None = None,
) -> tuple[str, dict[str, object] | None]:
    timestamp = _validate_timestamp(now or _now(), "operation timestamp")
    key = _load_key(state_dir, create=True)
    assert key is not None
    repo_facts = inspect_repo(repo, key)
    task_ref, thread_ref = _identity_refs(key, task_id, thread_id)
    label = _normalize_workstream(workstream)
    record_id = _pseudonym(
        key,
        "unpublished-queue-record",
        "\0".join((repo_facts.repo_ref, task_ref, label.casefold())),
        "cuq",
    )
    desired_state = _desired_queue_state(decision, scope, outcome)
    if local_commit_oid is not None and OID_RE.fullmatch(local_commit_oid) is None:
        raise QueueError("local_commit_oid must be a full Git OID")
    if remote_verified_oid is not None and OID_RE.fullmatch(remote_verified_oid) is None:
        raise QueueError("remote_verified_oid must be a full Git OID")

    with _queue_lock(state_dir, create=True, exclusive=True):
        records = _load_records_locked(state_dir)
        existing = records.get(record_id)
        if desired_state is None:
            if outcome == "remote_verified" and existing is not None:
                if remote_verified_oid is None:
                    raise QueueError("closing with remote_verified requires remote_verified_oid")
                if existing["queue_state"] in TERMINAL_STATES:
                    return "unchanged_terminal", existing
                closed = dict(existing)
                closed.update(
                    {
                        "publication_decision": decision,
                        "finalization_scope": scope,
                        "finalization_outcome": outcome,
                        "queue_state": "published",
                        "updated_at": timestamp,
                        "closure_evidence": {
                            "kind": "remote_verified",
                            "oid": remote_verified_oid,
                            "at": timestamp,
                        },
                    }
                )
                records[record_id] = validate_record(closed)
                _write_records_locked(state_dir, records)
                return "closed_published", records[record_id]
            return "no_active_record", None

        path_scope = build_path_scope(repo_facts.root, paths, key)
        candidate: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "record_id": record_id,
            "repo_ref": repo_facts.repo_ref,
            "task_ref": task_ref,
            "thread_ref": thread_ref,
            "workstream": label,
            "publication_decision": decision,
            "finalization_scope": scope,
            "finalization_outcome": outcome,
            "queue_state": desired_state,
            "base_head": repo_facts.head,
            "path_scope": path_scope,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_reviewed_at": None,
            "closure_evidence": None,
            "local_commit_oid": local_commit_oid,
        }
        if existing is not None:
            if existing["queue_state"] in TERMINAL_STATES:
                raise QueueError("terminal queue record cannot be reopened by upsert")
            candidate["created_at"] = existing["created_at"]
            candidate["last_reviewed_at"] = existing["last_reviewed_at"]
            unchanged = dict(candidate)
            unchanged["updated_at"] = existing["updated_at"]
            if unchanged == existing:
                return "unchanged", existing
        validated = validate_record(candidate)
        records[record_id] = validated
        _write_records_locked(state_dir, records)
        return ("updated" if existing is not None else "created"), validated


def summary(state_dir: Path, repo: Path) -> str:
    if not _ensure_state_dir(state_dir, create=False):
        return ""
    key = _load_key(state_dir, create=False)
    assert key is not None
    repo_ref = inspect_repo(repo, key).repo_ref
    with _queue_lock(state_dir, create=False, exclusive=False):
        records = _load_records_locked(state_dir)
    active = [
        record
        for record in records.values()
        if record["repo_ref"] == repo_ref and record["queue_state"] in ACTIVE_STATES
    ]
    if not active:
        return ""
    pending = sum(record["queue_state"] == "pending" for record in active)
    blocked = sum(record["queue_state"] == "blocked" for record in active)
    oldest = min(str(record["created_at"]) for record in active)[:10]
    result = (
        f"Unpublished backlog: {pending} pending, {blocked} blocked. "
        f"Oldest: {oldest}. Run queue review for details."
    )
    if len(result) > 300:
        raise QueueError("repo summary exceeded 300 characters")
    return result


def _tree_item(root: Path, oid: str, relative_path: str) -> dict[str, object]:
    raw = _git(root, "ls-tree", "-z", oid, "--", relative_path, binary=True)
    assert isinstance(raw, bytes)
    if not raw:
        return {"state": "deleted", "mode": None, "sha256": None}
    entries = [entry for entry in raw.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise QueueError(f"remote tree returned ambiguous path evidence: {relative_path}")
    header, returned_path = entries[0].split(b"\t", 1)
    if returned_path.decode("utf-8", errors="surrogateescape") != relative_path:
        raise QueueError(f"remote tree path mismatch: {relative_path}")
    fields = header.decode("ascii").split()
    if len(fields) != 3 or fields[1] != "blob" or OID_RE.fullmatch(fields[2]) is None:
        raise QueueError(f"remote tree object is not a blob: {relative_path}")
    content = _git(root, "cat-file", "blob", fields[2], binary=True)
    assert isinstance(content, bytes)
    mode = fields[0]
    return {
        "state": "symlink" if mode == "120000" else "file",
        "mode": mode,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _fingerprint_matches(expected: Mapping[str, object], actual: Mapping[str, object]) -> bool:
    return all(expected.get(name) == actual.get(name) for name in ("state", "mode", "sha256"))


def _scope_overlap_projection(
    record: Mapping[str, object],
    active_records: Sequence[Mapping[str, object]],
) -> tuple[str, list[dict[str, object]]]:
    scope = record["path_scope"]
    assert isinstance(scope, dict)
    items = scope["items"]
    assert isinstance(items, list)
    path_refs = {
        str(item["path_ref"])
        for item in items
        if isinstance(item, dict) and isinstance(item.get("path_ref"), str)
    }
    overlaps: list[dict[str, object]] = []
    projection = "none"
    for other in active_records:
        if other["record_id"] == record["record_id"]:
            continue
        other_scope = other["path_scope"]
        assert isinstance(other_scope, dict)
        other_items = other_scope["items"]
        assert isinstance(other_items, list)
        other_refs = {
            str(item["path_ref"])
            for item in other_items
            if isinstance(item, dict) and isinstance(item.get("path_ref"), str)
        }
        shared = sorted(path_refs & other_refs)
        if shared:
            relation = "confirmed"
            projection = "confirmed"
        elif "bounded" in {scope["kind"], other_scope["kind"]}:
            relation = "potential_bounded"
            if projection == "none":
                projection = "potential_bounded"
        else:
            continue
        overlaps.append(
            {
                "record_id": other["record_id"],
                "workstream": other["workstream"],
                "relation": relation,
                "overlapping_path_refs": shared,
            }
        )
    overlaps.sort(key=lambda item: str(item["record_id"]))
    return projection, overlaps


def _effective_state(record: Mapping[str, object], classification: str) -> str:
    if classification == "already_published_equivalent":
        return "published"
    if classification == "superseded_candidate":
        return "stale"
    return str(record["queue_state"])


def review(
    *,
    state_dir: Path,
    repo: Path,
    record_id: str | None = None,
    remote_verified_oid: str | None = None,
    now: str | None = None,
) -> list[dict[str, object]]:
    timestamp = _validate_timestamp(now or _now(), "operation timestamp")
    key = _load_key(state_dir, create=False)
    if key is None:
        return []
    repo_facts = inspect_repo(repo, key)
    if remote_verified_oid is not None:
        if OID_RE.fullmatch(remote_verified_oid) is None:
            raise QueueError("remote_verified_oid must be a full Git OID")
        if repo_facts.upstream_oid != remote_verified_oid:
            raise QueueError("remote_verified_oid must equal the configured upstream OID")

    with _queue_lock(state_dir, create=False, exclusive=True):
        records = _load_records_locked(state_dir)
        selected = [
            value
            for value in records.values()
            if value["repo_ref"] == repo_facts.repo_ref
            and value["queue_state"] in ACTIVE_STATES
            and (record_id is None or value["record_id"] == record_id)
        ]
        if record_id is not None and not selected:
            raise QueueError("active record was not found in the specified repository")
        active_records = [
            value
            for value in records.values()
            if value["repo_ref"] == repo_facts.repo_ref
            and value["queue_state"] in ACTIVE_STATES
        ]
        results: list[dict[str, object]] = []
        changed = False
        for record in selected:
            path_scope = record["path_scope"]
            assert isinstance(path_scope, dict)
            classification: str
            if path_scope["kind"] != "exact" or repo_facts.upstream_oid is None:
                classification = "needs_human_review"
            else:
                items = path_scope["items"]
                assert isinstance(items, list)
                upstream_matches = all(
                    _fingerprint_matches(
                        item,
                        _tree_item(
                            repo_facts.root,
                            repo_facts.upstream_oid,
                            str(item["relative_path"]),
                        ),
                    )
                    for item in items
                    if isinstance(item, dict)
                )
                worktree_matches = all(
                    _fingerprint_matches(
                        item,
                        _worktree_item(
                            repo_facts.root,
                            str(item["relative_path"]),
                            key,
                        ),
                    )
                    for item in items
                    if isinstance(item, dict)
                )
                if upstream_matches and remote_verified_oid is not None:
                    classification = "already_published_equivalent"
                elif upstream_matches:
                    classification = "needs_human_review"
                elif record["queue_state"] == "blocked":
                    classification = "still_blocked"
                elif worktree_matches:
                    classification = "still_pending"
                else:
                    classification = "superseded_candidate"

            updated = dict(record)
            updated["last_reviewed_at"] = timestamp
            updated["updated_at"] = timestamp
            if classification == "already_published_equivalent":
                updated["queue_state"] = "published"
                updated["closure_evidence"] = {
                    "kind": "already_published_equivalent",
                    "oid": remote_verified_oid,
                    "at": timestamp,
                }
            records[str(record["record_id"])] = validate_record(updated)
            changed = True
            overlap_state, overlaps = _scope_overlap_projection(
                record,
                active_records,
            )
            results.append(
                {
                    "record_id": record["record_id"],
                    "result": classification,
                    "queue_state": updated["queue_state"],
                    "effective_state": _effective_state(record, classification),
                    "overlap_state": overlap_state,
                    "overlaps": overlaps,
                    "head": repo_facts.head,
                    "upstream": repo_facts.upstream_ref,
                    "upstream_oid": repo_facts.upstream_oid,
                    "ahead": repo_facts.ahead,
                    "behind": repo_facts.behind,
                }
            )
        if changed:
            _write_records_locked(state_dir, records)
    return results


def close(
    *,
    state_dir: Path,
    record_id: str,
    state: str,
    evidence_kind: str,
    oid: str | None = None,
    now: str | None = None,
) -> dict[str, object]:
    if state not in TERMINAL_STATES:
        raise QueueError("close state must be published, superseded, or dismissed")
    allowed = {
        "published": {"remote_verified", "already_published_equivalent"},
        "superseded": {"superseded_by_task", "superseded_by_commit", "manual_confirmation"},
        "dismissed": {"explicit_user_dismissal", "invalid_record", "permanent_local_only"},
    }
    if evidence_kind not in allowed[state]:
        raise QueueError(f"invalid closure evidence for {state}: {evidence_kind}")
    if state == "published" and (oid is None or OID_RE.fullmatch(oid) is None):
        raise QueueError("published closure requires a full verified Git OID")
    if oid is not None and OID_RE.fullmatch(oid) is None:
        raise QueueError("closure OID must be a full Git OID")
    timestamp = _validate_timestamp(now or _now(), "operation timestamp")
    _load_key(state_dir, create=False)
    with _queue_lock(state_dir, create=False, exclusive=True):
        records = _load_records_locked(state_dir)
        if record_id not in records:
            raise QueueError("queue record was not found")
        record = records[record_id]
        if record["queue_state"] in TERMINAL_STATES:
            if record["queue_state"] == state and record["closure_evidence"] == {
                "kind": evidence_kind,
                "oid": oid,
                "at": record["closure_evidence"]["at"],
            }:
                return record
            raise QueueError("terminal queue record cannot transition again")
        updated = dict(record)
        updated.update(
            {
                "queue_state": state,
                "updated_at": timestamp,
                "closure_evidence": {"kind": evidence_kind, "oid": oid, "at": timestamp},
            }
        )
        records[record_id] = validate_record(updated)
        _write_records_locked(state_dir, records)
        return records[record_id]


def validate_queue(state_dir: Path) -> tuple[int, int]:
    if not _ensure_state_dir(state_dir, create=False):
        return 0, 0
    _load_key(state_dir, create=False)
    with _queue_lock(state_dir, create=False, exclusive=False):
        records = _load_records_locked(state_dir)
    active = sum(record["queue_state"] in ACTIVE_STATES for record in records.values())
    return len(records), active


def migration_dry_run(
    *, state_dir: Path, source: Path, output: Path | None = None
) -> dict[str, object]:
    key = _load_key(state_dir, create=True)
    assert key is not None
    raw = source.read_bytes()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise QueueError("migration source is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "source_cutoff",
        "source_name",
        "entries",
    }:
        raise QueueError("migration source has unexpected fields")
    if payload["schema_version"] != MIGRATION_SOURCE_SCHEMA:
        raise QueueError("migration source schema is unsupported")
    _validate_timestamp(payload["source_cutoff"], "migration source_cutoff")
    source_name = payload["source_name"]
    if not isinstance(source_name, str) or not source_name or len(source_name) > 128:
        raise QueueError("migration source_name is invalid")
    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) > 100:
        raise QueueError("migration entries must be a list of at most 100 items")

    projected: list[dict[str, object]] = []
    for item in entries:
        expected = {
            "legacy_ref",
            "repo",
            "workstream",
            "proposed_state",
            "confidence",
            "task_id",
            "thread_id",
            "paths",
        }
        if not isinstance(item, dict) or set(item) != expected:
            raise QueueError("migration entry has unexpected fields")
        legacy = item["legacy_ref"]
        if not isinstance(legacy, str) or not legacy or len(legacy) > 128:
            raise QueueError("migration legacy_ref is invalid")
        confidence = item["confidence"]
        state = item["proposed_state"]
        if confidence not in CONFIDENCE_LEVELS:
            raise QueueError("migration confidence is invalid")
        if state not in ACTIVE_STATES:
            raise QueueError("migration proposed_state must be pending or blocked")
        workstream = _normalize_workstream(str(item["workstream"]))
        repo_facts = inspect_repo(Path(str(item["repo"])), key)
        task_id = item["task_id"]
        thread_id = item["thread_id"]
        paths = item["paths"]
        if task_id is not None and not isinstance(task_id, str):
            raise QueueError("migration task_id must be a string or null")
        if thread_id is not None and not isinstance(thread_id, str):
            raise QueueError("migration thread_id must be a string or null")
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise QueueError("migration paths must be a string list")
        if confidence in {"low", "unknown"}:
            action = "excluded_low_confidence"
            reasons = ["confidence_not_sufficient"]
        else:
            missing = []
            if not task_id:
                missing.append("task_id_missing")
            if not thread_id:
                missing.append("thread_id_missing")
            if not paths:
                missing.append("machine_readable_scope_missing")
            action = "human_confirmation_required" if missing else "eligible_for_import"
            reasons = missing
        task_ref = (
            _pseudonym(key, "unpublished-queue-task", task_id, "task")
            if isinstance(task_id, str) and task_id
            else None
        )
        thread_ref = (
            _pseudonym(key, "unpublished-queue-thread", thread_id, "thr")
            if isinstance(thread_id, str) and thread_id
            else None
        )
        normalized_paths = sorted({_normalize_relative_path(path) for path in paths})
        projected.append(
            {
                "migration_ref": _pseudonym(
                    key, "unpublished-queue-migration-entry", legacy, "migration"
                ),
                "repo_ref": repo_facts.repo_ref,
                "task_ref": task_ref,
                "thread_ref": thread_ref,
                "workstream": workstream,
                "proposed_state": state,
                "confidence": confidence,
                "path_count": len(normalized_paths),
                "path_set_sha256": _digest_parts(normalized_paths),
                "action": action,
                "reasons": reasons,
            }
        )
    projected.sort(key=lambda item: (str(item["repo_ref"]), str(item["migration_ref"])))
    result = {
        "schema_version": MIGRATION_RESULT_SCHEMA,
        "source_name": source_name,
        "source_cutoff": payload["source_cutoff"],
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "candidate_count": len(projected),
        "eligible_import_count": sum(
            item["action"] == "eligible_for_import" for item in projected
        ),
        "human_confirmation_count": sum(
            item["action"] == "human_confirmation_required" for item in projected
        ),
        "excluded_count": sum(
            item["action"] == "excluded_low_confidence" for item in projected
        ),
        "formal_import_performed": False,
        "entries": projected,
    }
    if output is not None:
        output_parent = output.parent
        if output_parent.resolve(strict=False) != state_dir.resolve(strict=False):
            raise QueueError("migration output must be written directly inside the queue state directory")
        _ensure_state_dir(state_dir, create=True)
        if output.exists() or output.is_symlink():
            _owned_mode(output, 0o600, "migration output")
        _atomic_write(output, _canonical_json(result) + b"\n")
        _owned_mode(output, 0o600, "migration output")
    return result


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id")
    parser.add_argument("--thread-id")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")


def _resolve_identity(arguments: argparse.Namespace) -> tuple[str, str]:
    """Resolve one task/thread identity for any provider.

    Explicit identity is the provider-neutral contract: a caller that already knows its
    own task and thread passes them and no rollout is read. Deriving the task from Codex
    rollout lifecycle is a Codex-only convenience, so a thread with no rollout is told to
    supply the identity instead of failing on a Codex-internal condition.
    """
    thread_id = (
        arguments.thread_id
        or os.environ.get("AGENT_THREAD_ID")
        or os.environ.get("CODEX_THREAD_ID")
    )
    if not thread_id:
        raise QueueError("thread identity is unavailable; pass --thread-id")
    task_id = arguments.task_id or os.environ.get("AGENT_TASK_ID")
    if task_id:
        return task_id, thread_id
    if not codex_rollouts(arguments.codex_home, thread_id):
        raise QueueError(
            "no Codex rollout records this thread; pass --task-id (or set AGENT_TASK_ID) "
            "to record a task from another provider"
        )
    return resolve_current_task_id(arguments.codex_home, thread_id), thread_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir())
    subparsers = parser.add_subparsers(dest="command", required=True)

    upsert_parser = subparsers.add_parser("upsert")
    upsert_parser.add_argument("--repo", type=Path, required=True)
    _add_identity_arguments(upsert_parser)
    upsert_parser.add_argument("--workstream", required=True)
    upsert_parser.add_argument("--publication-decision", required=True)
    upsert_parser.add_argument("--finalization-scope", required=True)
    upsert_parser.add_argument("--finalization-outcome", required=True)
    upsert_parser.add_argument("--local-commit-oid")
    upsert_parser.add_argument("--remote-verified-oid")
    upsert_parser.add_argument("--path", action="append", default=[])

    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("--repo", type=Path, required=True)

    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("--repo", type=Path, required=True)
    review_parser.add_argument("--record")
    review_parser.add_argument("--remote-verified-oid")

    close_parser = subparsers.add_parser("close")
    close_parser.add_argument("--record", required=True)
    close_parser.add_argument("--state", required=True)
    close_parser.add_argument("--evidence-kind", required=True)
    close_parser.add_argument("--oid")

    subparsers.add_parser("validate")

    migration_parser = subparsers.add_parser("migration-dry-run")
    migration_parser.add_argument("--source", type=Path, required=True)
    migration_parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        state_dir = arguments.state_dir.expanduser().resolve(strict=False)
        if arguments.command == "upsert":
            task_id, thread_id = _resolve_identity(arguments)
            action, record = upsert(
                state_dir=state_dir,
                repo=arguments.repo,
                task_id=task_id,
                thread_id=thread_id,
                workstream=arguments.workstream,
                decision=arguments.publication_decision,
                scope=arguments.finalization_scope,
                outcome=arguments.finalization_outcome,
                paths=arguments.path,
                local_commit_oid=arguments.local_commit_oid,
                remote_verified_oid=arguments.remote_verified_oid,
            )
            print(
                json.dumps(
                    {
                        "action": action,
                        "record_id": record["record_id"] if record is not None else None,
                        "queue_state": record["queue_state"] if record is not None else None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif arguments.command == "summary":
            value = summary(state_dir, arguments.repo)
            if value:
                print(value)
        elif arguments.command == "review":
            print(
                json.dumps(
                    review(
                        state_dir=state_dir,
                        repo=arguments.repo,
                        record_id=arguments.record,
                        remote_verified_oid=arguments.remote_verified_oid,
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif arguments.command == "close":
            record = close(
                state_dir=state_dir,
                record_id=arguments.record,
                state=arguments.state,
                evidence_kind=arguments.evidence_kind,
                oid=arguments.oid,
            )
            print(
                json.dumps(
                    {"record_id": record["record_id"], "queue_state": record["queue_state"]},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif arguments.command == "validate":
            records, active = validate_queue(state_dir)
            print(f"valid records={records} active={active}")
        else:
            result = migration_dry_run(
                state_dir=state_dir,
                source=arguments.source,
                output=arguments.output,
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except (QueueError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
