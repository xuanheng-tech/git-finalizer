# GF-INTEGRATION-ADAPTER: snapshot-runner-evidence (single site: Snapshot Runner read verification)
"""Verify Snapshot Runner evidence before initial unborn-repository publication."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, NoReturn

from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import clean_git_env, find_git
from git_finalizer.json_io import loads_unique

EVIDENCE_SCHEMA_VERSION = 1
DATA_FIELDS = (
    "status_short",
    "staged_diff",
    "unstaged_diff",
    "file_context",
    "baseline_kind",
    "baseline_oid",
    "initial_publication",
)
RUNNER_ERROR_CODE = re.compile(r"workflow_failed: ([A-Z][A-Z_]{0,63}):")
VERSION_TUPLE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_GENERATED_FILES = 500
MAX_GENERATED_BYTES = 8 * 1024 * 1024
MAX_SCAN_BYTES = 25 * 1024 * 1024


def fail(message: str) -> NoReturn:
    raise FinalizerError(
        ErrorCode.SNAPSHOT_EVIDENCE_REJECTED,
        f"snapshot evidence rejected: {message}",
        final_phase="local_validation",
    )


def _nonnegative(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    posix = PurePosixPath(value)
    return (
        value not in {".", "..", ".git"}
        and not posix.is_absolute()
        and not value.startswith(("/", "./"))
        and not value.endswith("/")
        and "\\" not in value
        and "\0" not in value
        and ".." not in posix.parts
        and ".git" not in posix.parts
    )


def _git_command(
    repo: Path,
    *arguments: str,
    git_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    git_bin = find_git(git_env.get("PATH") if git_env else None)
    env = clean_git_env(git_env)
    try:
        return subprocess.run(
            [git_bin, "-C", str(repo), *arguments],
            capture_output=True,
            timeout=30,
            env=env,
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"Git inspection failed: {exc}")


def _parse_version_tuple(version_str: object) -> tuple[int, int, int] | None:
    if not isinstance(version_str, str):
        return None
    match = VERSION_TUPLE_RE.search(version_str)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _run_runner(runner: str, arguments: list[str]) -> dict[str, Any]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [runner, *arguments],
            capture_output=True,
            timeout=30,
            env=env,
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        fail("Snapshot Runner invocation failed")
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        match = RUNNER_ERROR_CODE.search(stderr)
        if match:
            fail(f"Snapshot Runner refused the evidence read ({match.group(1)})")
        fail("Snapshot Runner refused the evidence read")
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        fail("Snapshot Runner evidence exceeded the read budget")
    try:
        document = loads_unique(result.stdout, allow_nonfinite=False)
    except (UnicodeDecodeError, ValueError):
        fail("Snapshot Runner returned invalid evidence JSON")
    if not isinstance(document, dict):
        fail("Snapshot Runner evidence root must be an object")
    return document


def _validate_envelope(
    document: dict[str, Any],
    snapshot_id: str,
    repository_name: str,
    expected_selector: dict[str, Any],
) -> dict[str, Any]:
    if (
        document.get("evidence_schema_version") != EVIDENCE_SCHEMA_VERSION
        or document.get("command") != "diff-audit"
        or document.get("snapshot_id") != snapshot_id
        or document.get("repository") != repository_name
        or document.get("status") != "complete"
        or document.get("truncated") is not False
        or document.get("evidence_gap") is not False
        or document.get("found") is not True
    ):
        fail("snapshot metadata does not establish complete coverage")
    if document.get("selector") != expected_selector:
        fail("snapshot read selector does not match request")
    evidence = document.get("evidence")
    if not isinstance(evidence, dict):
        fail("snapshot evidence payload is missing")
    return evidence


def _read_worktree(repo: Path, path: str) -> tuple[bytes, bool]:
    target = repo / path
    if target.is_symlink() or not target.is_file():
        fail(f"candidate worktree target is missing or non-regular: {path}")
    info = target.lstat()
    if info.st_size > MAX_SCAN_BYTES:
        fail(f"candidate file exceeds scan byte budget: {path}")
    return target.read_bytes(), bool(stat.S_IMODE(info.st_mode) & 0o111)


def _read_git_version(
    repo: Path,
    stage: str,
    path: str,
    expected_bytes: int | None = None,
    *,
    git_env: dict[str, str] | None = None,
) -> tuple[bytes, bool]:
    if stage == "index":
        ls = _git_command(
            repo, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", path, git_env=git_env
        )
    else:
        ls = _git_command(
            repo, "--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", path, git_env=git_env
        )
    if ls.returncode != 0:
        fail(f"unable to inspect {stage} entry for {path}")
    entries = [part for part in ls.stdout.split(b"\0") if part]
    if len(entries) != 1:
        fail(f"missing or ambiguous {stage} entry for {path}")
    line = entries[0].decode("utf-8", errors="strict")
    header, _, rec_path = line.partition("\t")
    if rec_path != path:
        fail(f"{stage} path mismatch for {path}")
    tokens = header.split()
    if stage == "index":
        if len(tokens) != 3 or tokens[2] != "0":
            fail(f"invalid index stage entry for {path}")
        mode, oid = tokens[0], tokens[1]
    else:
        if len(tokens) != 3 or tokens[1] != "blob":
            fail(f"invalid HEAD tree entry for {path}")
        mode, oid = tokens[0], tokens[2]
    if mode not in {"100644", "100755"}:
        fail(f"unsupported Git file mode {mode} for {path}")

    # Check object size via cat-file -s BEFORE materializing the blob in memory.
    size_res = _git_command(repo, "cat-file", "-s", oid, git_env=git_env)
    if size_res.returncode != 0:
        fail(f"unable to read blob size for {path}")
    try:
        actual_size = int(size_res.stdout.decode("ascii").strip())
    except ValueError:
        fail(f"invalid blob size response for {path}")
    if actual_size > MAX_SCAN_BYTES:
        fail(f"{stage} blob exceeds scan byte budget: {path}")
    if expected_bytes is not None and actual_size != expected_bytes:
        fail(f"{stage} file byte count changed after snapshot: {path}")

    blob = _git_command(repo, "cat-file", "blob", oid, git_env=git_env)
    if blob.returncode != 0:
        fail(f"unable to read {stage} blob for {path}")
    return blob.stdout, mode == "100755"


def _workspace_digest(records: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for record in sorted(records, key=lambda item: str(item["path"])):
        h.update(str(record["path"]).encode("utf-8"))
        h.update(b"\0")
        h.update(str(record["bytes"]).encode("ascii"))
        h.update(b"\0")
        h.update(str(record["sha256"]).encode("ascii"))
        h.update(b"\0")
        h.update(b"1" if record["executable"] is True else b"0")
        h.update(b"\n")
    return h.hexdigest()


def verify_snapshot_evidence(
    snapshot_id: str,
    repo: Path,
    stage: str,
    requested_paths: list[str],
    *,
    git_env: dict[str, str] | None = None,
) -> None:
    """Verify Snapshot Runner evidence matches all requested files at `stage` (`worktree|index|head`)."""
    if stage not in {"worktree", "index", "head"}:
        fail(f"invalid snapshot verification stage: {stage}")
    if not requested_paths:
        fail("snapshot verification requires explicit paths")

    runner = shutil.which("snapshot-runner")
    if not runner:
        fail("Snapshot Runner is unavailable on PATH")

    repo_resolved = repo.resolve()
    repo_name = repo_resolved.name

    index_doc = _run_runner(runner, ["read", snapshot_id, "--repo", str(repo_resolved)])
    index_evidence = _validate_envelope(index_doc, snapshot_id, repo_name, {"kind": "index"})
    fields_list = index_evidence.get("fields")
    if not isinstance(fields_list, list):
        fail("snapshot index fields missing")
    runner_version = _parse_version_tuple(index_doc.get("runner_version"))
    allowed_fields = (
        set(DATA_FIELDS) | {"current_branch", "head"}
        if runner_version is not None and runner_version >= (2, 6, 0)
        else set(DATA_FIELDS)
    )
    available_fields = [item.get("field") for item in fields_list if isinstance(item, dict)]
    if (
        len(available_fields) != len(fields_list)
        or len(set(available_fields)) != len(available_fields)
        or not set(DATA_FIELDS).issubset(available_fields)
        or not set(available_fields).issubset(allowed_fields)
    ):
        fail("snapshot fields do not match required initial publication coverage")
    data: dict[str, Any] = {}
    # Use SR 2.6.0 batch `--fields` when runner >= 2.6.0, falling back to `--field` for SR 2.5.13/stubs.
    if runner_version is not None and runner_version >= (2, 6, 0):
        batch_arg = ",".join(DATA_FIELDS)
        batch_doc = _run_runner(
            runner,
            ["read", snapshot_id, "--repo", str(repo_resolved), "--fields", batch_arg],
        )
        batch_evidence = _validate_envelope(
            batch_doc,
            snapshot_id,
            repo_name,
            {"kind": "fields", "values": list(DATA_FIELDS)},
        )
        values = batch_evidence.get("fields")
        if not isinstance(values, dict) or set(values) != set(DATA_FIELDS):
            fail("batch fields payload mismatch")
        data = dict(values)
    else:
        for field in DATA_FIELDS:
            field_doc = _run_runner(
                runner, ["read", snapshot_id, "--repo", str(repo_resolved), "--field", field]
            )
            field_evidence = _validate_envelope(
                field_doc, snapshot_id, repo_name, {"kind": "field", "value": field}
            )
            if field_evidence.get("field") != field or "value" not in field_evidence:
                fail(f"snapshot field selector mismatch: {field}")
            data[field] = field_evidence["value"]

    empty_tree_res = _git_command(repo, "hash-object", "-t", "tree", "/dev/null", git_env=git_env)
    empty_tree = empty_tree_res.stdout.decode("ascii").strip()
    if data.get("baseline_kind") != "empty_tree" or data.get("baseline_oid") != empty_tree:
        fail("snapshot baseline is not the empty tree")

    publication = data.get("initial_publication")
    if not isinstance(publication, dict):
        fail("initial_publication metadata is missing")

    records = publication.get("files")
    if not isinstance(records, list) or not records:
        fail("initial_publication files are missing")
    if publication.get("changed_file_count") != len(requested_paths):
        fail("snapshot changed_file_count differs from explicit scope")

    expected_set = set(requested_paths)
    observed_paths: list[str] = []
    content_records: dict[str, dict[str, Any]] = {}
    generated_count = 0
    generated_bytes = 0
    total_snapshot_bytes = 0

    for record in records:
        if not isinstance(record, dict):
            fail("initial_publication file entry is invalid")
        path = record.get("path")
        sha = record.get("sha256")
        byte_count = record.get("bytes")
        executable = record.get("executable")
        coverage = record.get("coverage")
        if (
            not _safe_path(path)
            or not isinstance(sha, str)
            or not isinstance(executable, bool)
            or not _nonnegative(byte_count)
            or coverage not in {"content", "generated_manifest"}
        ):
            fail("initial_publication file record is malformed")
        assert isinstance(path, str) and isinstance(byte_count, int)
        observed_paths.append(path)
        total_snapshot_bytes += byte_count

        if stage == "worktree":
            actual_bytes, actual_exec = _read_worktree(repo, path)
        else:
            actual_bytes, actual_exec = _read_git_version(
                repo, stage, path, expected_bytes=byte_count, git_env=git_env
            )

        if len(actual_bytes) != byte_count or hashlib.sha256(actual_bytes).hexdigest() != sha:
            fail(f"file changed after snapshot was collected: {path}")
        if actual_exec != executable:
            fail(f"file executable mode changed after snapshot: {path}")

        if coverage == "content":
            content_records[path] = {"bytes": actual_bytes, "size": byte_count}
        else:
            generated_count += 1
            generated_bytes += byte_count

    if (
        len(observed_paths) != len(set(observed_paths))
        or set(observed_paths) != expected_set
        or total_snapshot_bytes > MAX_SNAPSHOT_BYTES
        or _workspace_digest(records) != publication.get("workspace_sha256")
    ):
        fail("snapshot does not exactly cover the explicit publication paths")

    # Verify file_context paths AND actual content against the workspace/index/head bytes.
    contexts = data.get("file_context")
    if not isinstance(contexts, list):
        fail("snapshot content contexts are invalid")
    context_paths: list[str] = []
    for context in contexts:
        if (
            not isinstance(context, dict)
            or set(context) not in ({"path", "content"}, {"path", "content", "source", "executable"})
            or not isinstance(context.get("path"), str)
            or not isinstance(context.get("content"), str)
        ):
            fail("snapshot content context is invalid")
        if "source" in context and (
            context.get("source") not in {"untracked", "worktree"}
            or not isinstance(context.get("executable"), bool)
        ):
            fail("snapshot content context source is invalid")
        ctx_path = str(context["path"])
        ctx_content = str(context["content"])
        if ctx_path in content_records:
            if "[REDACTED_" in ctx_content:
                fail(f"snapshot content context contains redacted secret markers: {ctx_path}")
            try:
                actual_text = content_records[ctx_path]["bytes"].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                fail(f"snapshot content file is not valid UTF-8: {ctx_path}")
            if ctx_content != actual_text and "<<ABS_PATH:" not in ctx_content:
                fail(f"snapshot content context does not match candidate file content: {ctx_path}")
        context_paths.append(ctx_path)

    if len(context_paths) != len(set(context_paths)) or set(context_paths) != set(content_records):
        fail("snapshot content contexts do not match content coverage")
    print(f"snapshot evidence verified: source={stage} covered={len(observed_paths)}/{len(requested_paths)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4:
        print(
            "usage: snapshot_verify <snapshot_id> <repo> <worktree|index|head> <path>...",
            file=sys.stderr,
        )
        return 2
    snapshot_id, repo_raw, stage, *paths = args
    try:
        verify_snapshot_evidence(snapshot_id, Path(repo_raw), stage, paths)
        return 0
    except FinalizerError as exc:
        print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
