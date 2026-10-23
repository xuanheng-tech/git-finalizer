#!/usr/bin/env python3
"""Verify one complete Snapshot Runner initial-publication artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_OUTPUT_BYTES = MAX_SNAPSHOT_BYTES + 1024 * 1024
MAX_CONTENT_FILES = 128
MAX_GENERATED_FILES = 512
RUNNER_TIMEOUT_SECONDS = 120
SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
# GF-INTEGRATION-ADAPTER: snapshot-runner-evidence (single site: public read contract)
# Snapshot Runner validates its own store, artifact era and format before it answers;
# Finalizer reads only the published `snapshot-runner read` JSON, evidence schema 1.
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


def fail(message: str) -> None:
    raise SystemExit(f"snapshot evidence rejected: {message}")


def nonnegative(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 2**63 - 1
    )


def safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return (
        value not in {".", "..", ".git"}
        and not value.startswith(".git/")
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and not any(character in value for character in ("\x00", "\n", "\r", "\t"))
    )


def read_evidence(runner: str, field: str | None) -> object:
    """Return one validated `snapshot-runner read` evidence body for this snapshot."""
    arguments = [runner, "read", snapshot_id, "--repo", os.fspath(repo)]
    if field is not None:
        arguments += ["--field", field]
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            timeout=RUNNER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("Snapshot Runner evidence read did not complete")
    if completed.returncode != 0:
        first = completed.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        code = RUNNER_ERROR_CODE.match(first[0]) if first else None
        reason = code.group(1) if code else "unknown"
        fail(f"Snapshot Runner refused the evidence read ({reason})")
    if len(completed.stdout) > MAX_EVIDENCE_OUTPUT_BYTES:
        fail("Snapshot Runner evidence exceeds its hard limit")
    try:
        document = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("Snapshot Runner evidence is not valid JSON")
    selector = {"kind": "index"} if field is None else {"kind": "field", "value": field}
    if (
        not isinstance(document, dict)
        or type(document.get("evidence_schema_version")) is not int
        or document["evidence_schema_version"] != EVIDENCE_SCHEMA_VERSION
        or document.get("snapshot_id") != snapshot_id
        or document.get("command") != "diff-audit"
        or document.get("repository") != repo.name
        or document.get("selector") != selector
        or document.get("status") != "complete"
        or document.get("truncated") is not False
        or document.get("evidence_gap") is not False
        or document.get("found") is not True
        or not isinstance(document.get("evidence"), dict)
    ):
        fail("snapshot evidence is incomplete or incompatible")
    evidence = document["evidence"]
    if field is None:
        return evidence
    if set(evidence) != {"field", "value"} or evidence["field"] != field:
        fail("snapshot evidence field does not match the request")
    return evidence["value"]


def workspace_digest(records: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: str(item["path"])):
        digest.update(str(record["path"]).encode())
        digest.update(b"\0")
        digest.update(str(record["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(b"1" if record["executable"] is True else b"0")
        digest.update(b"\n")
    return digest.hexdigest()


def run_git(arguments: list[str], *, input_bytes: bytes | None = None) -> bytes:
    environment = {
        "HOME": os.environ.get("HOME", ""),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/usr/bin/git", "-C", os.fspath(repo), *arguments],
        input=input_bytes,
        capture_output=True,
        env=environment,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or result.stderr:
        fail("Git evidence lookup failed")
    return result.stdout


def read_worktree(relative: str, maximum: int) -> tuple[int, str, bool]:
    parts = PurePosixPath(relative).parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(repo, directory_flags)
    except OSError:
        fail("repository root could not be safely opened")
    try:
        for component in parts[:-1]:
            try:
                metadata = os.stat(
                    component, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    fail("publication path contains a symlink or special directory")
                next_descriptor = os.open(
                    component, directory_flags, dir_fd=directory_descriptor
                )
            except OSError:
                fail("publication path directory could not be safely opened")
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        try:
            before = os.stat(
                parts[-1], dir_fd=directory_descriptor, follow_symlinks=False
            )
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                fail("publication path is not a regular non-symlink file")
            flags = (
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(parts[-1], flags, dir_fd=directory_descriptor)
            opened = os.fstat(descriptor)
        except OSError:
            fail("publication file could not be safely opened")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            fail("publication file changed during safe open")
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    fail("publication file exceeds snapshot size evidence")
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_size,
            opened.st_mtime_ns,
        ) or total != opened.st_size:
            fail("publication file changed while reading")
        return total, digest.hexdigest(), bool(opened.st_mode & 0o111)
    finally:
        os.close(directory_descriptor)


def read_git_version(
    relative: str, expected_size: int, source: str
) -> tuple[int, str, bool]:
    if source == "index":
        raw_entry = run_git(
            ["--literal-pathspecs", "ls-files", "-s", "-z", "--", relative]
        )
        entries = [entry for entry in raw_entry.split(b"\0") if entry]
        if len(entries) != 1 or b"\t" not in entries[0]:
            fail("snapshot path is not one unambiguous stage-zero index entry")
        metadata, encoded_path = entries[0].split(b"\t", 1)
        if encoded_path.decode("utf-8", errors="strict") != relative:
            fail("index path does not match snapshot evidence")
        fields = metadata.decode("ascii", errors="strict").split()
        if len(fields) != 3 or fields[2] != "0":
            fail("snapshot path is not one stage-zero index entry")
        mode, object_id = fields[:2]
    else:
        raw_entry = run_git(
            ["--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", relative]
        )
        entries = [entry for entry in raw_entry.split(b"\0") if entry]
        if len(entries) != 1 or b"\t" not in entries[0]:
            fail("snapshot path is not one unambiguous HEAD tree entry")
        metadata, encoded_path = entries[0].split(b"\t", 1)
        if encoded_path.decode("utf-8", errors="strict") != relative:
            fail("HEAD path does not match snapshot evidence")
        fields = metadata.decode("ascii", errors="strict").split()
        if len(fields) != 3 or fields[1] != "blob":
            fail("snapshot path is not a regular HEAD blob")
        mode, _kind, object_id = fields
    if mode not in {"100644", "100755"}:
        fail("snapshot path is a symlink or non-regular Git entry")
    content = run_git(["cat-file", "blob", object_id])
    if len(content) > expected_size:
        fail("Git blob exceeds snapshot size evidence")
    return len(content), hashlib.sha256(content).hexdigest(), mode == "100755"


if len(sys.argv) < 5:
    fail("verifier arguments are incomplete")
snapshot_id, repo_raw, source, *explicit_paths = sys.argv[1:]
if SNAPSHOT_ID.fullmatch(snapshot_id) is None or source not in {
    "worktree",
    "index",
    "head",
}:
    fail("snapshot id or verification source is invalid")
if not explicit_paths or any(not safe_path(path) for path in explicit_paths):
    fail("explicit publication paths are invalid")
if len(explicit_paths) != len(set(explicit_paths)):
    fail("explicit publication paths are duplicated")
try:
    repo = Path(repo_raw).resolve(strict=True)
except OSError:
    fail("repository root is unavailable")
if not repo.is_dir() or os.fspath(repo) != repo_raw:
    fail("repository root is not canonical")

runner = shutil.which("snapshot-runner")
if runner is None:
    fail("Snapshot Runner is unavailable on PATH")
evidence_index = read_evidence(runner, None)
fields = evidence_index.get("fields")
if (
    not isinstance(fields, list)
    or not all(isinstance(entry, dict) for entry in fields)
    or sorted(str(entry.get("field")) for entry in fields) != sorted(DATA_FIELDS)
):
    fail("snapshot evidence fields are not initial-publication evidence")
data = {field: read_evidence(runner, field) for field in DATA_FIELDS}
if (
    not isinstance(data.get("status_short"), str)
    or data.get("staged_diff") != ""
    or data.get("unstaged_diff") != ""
    or data.get("baseline_kind") != "empty_tree"
    or not isinstance(data.get("baseline_oid"), str)
    or run_git(["hash-object", "-t", "tree", "--stdin"], input_bytes=b"")
    .decode()
    .strip()
    != data.get("baseline_oid")
):
    fail("snapshot is not sealed to this unborn empty-tree baseline")

publication = data.get("initial_publication")
publication_keys = {
    "schema_version",
    "mode",
    "complete",
    "sensitive_scan",
    "changed_file_count",
    "covered_file_count",
    "content_file_count",
    "generated_file_count",
    "workspace_sha256",
    "files",
    "generated_trees",
}
if not isinstance(publication, dict) or set(publication) != publication_keys:
    fail("initial publication evidence schema is invalid")
records = publication.get("files")
trees = publication.get("generated_trees")
counts = (
    publication.get("changed_file_count"),
    publication.get("covered_file_count"),
    publication.get("content_file_count"),
    publication.get("generated_file_count"),
)
if (
    publication.get("schema_version") != 1
    or publication.get("mode") != "unborn"
    or publication.get("complete") is not True
    or publication.get("sensitive_scan") != "complete"
    or any(not nonnegative(count) for count in counts)
    or not isinstance(records, list)
    or not isinstance(trees, list)
    or not isinstance(publication.get("workspace_sha256"), str)
    or SNAPSHOT_ID.fullmatch(str(publication.get("workspace_sha256"))) is None
):
    fail("initial publication evidence is not complete")
changed_count, covered_count, content_count, generated_count = map(int, counts)
if (
    changed_count != covered_count
    or covered_count != len(records)
    or covered_count != len(explicit_paths)
    or covered_count != content_count + generated_count
    or content_count > MAX_CONTENT_FILES
    or generated_count > MAX_GENERATED_FILES
):
    fail("initial publication evidence counts do not cover the explicit scope")

content_keys = {"path", "bytes", "sha256", "executable", "coverage"}
generated_keys = {*content_keys, "manifest_path"}
record_paths: list[str] = []
content_paths: set[str] = set()
generated_by_manifest: dict[str, list[dict[str, object]]] = {}
generated_bytes = 0
for record in records:
    if not isinstance(record, dict):
        fail("publication file record is invalid")
    coverage = record.get("coverage")
    expected_keys = content_keys if coverage == "content" else generated_keys
    path = record.get("path")
    byte_size = record.get("bytes")
    sha256 = record.get("sha256")
    if (
        set(record) != expected_keys
        or coverage not in {"content", "generated_manifest"}
        or not safe_path(path)
        or not nonnegative(byte_size)
        or int(byte_size) > MAX_SNAPSHOT_BYTES
        or not isinstance(sha256, str)
        or SNAPSHOT_ID.fullmatch(sha256) is None
        or not isinstance(record.get("executable"), bool)
    ):
        fail("publication file record is invalid")
    assert isinstance(path, str)
    record_paths.append(path)
    if coverage == "content":
        content_paths.add(path)
    else:
        manifest_path = record.get("manifest_path")
        if (
            not safe_path(manifest_path)
            or PurePosixPath(path).suffix.lower() != ".json"
            or path == manifest_path
            or record.get("executable") is not False
        ):
            fail("generated-manifest file record is invalid")
        assert isinstance(manifest_path, str)
        generated_by_manifest.setdefault(manifest_path, []).append(record)
        generated_bytes += int(byte_size)
if (
    record_paths != sorted(record_paths)
    or len(record_paths) != len(set(record_paths))
    or set(record_paths) != set(explicit_paths)
    or len(content_paths) != content_count
    or sum(len(group) for group in generated_by_manifest.values()) != generated_count
    or generated_bytes > MAX_SNAPSHOT_BYTES
    or workspace_digest(records) != publication.get("workspace_sha256")
):
    fail("snapshot does not exactly cover the explicit publication paths")

contexts = data.get("file_context")
if not isinstance(contexts, list):
    fail("snapshot content contexts are invalid")
context_paths: list[str] = []
for context in contexts:
    if (
        not isinstance(context, dict)
        or set(context)
        not in ({"path", "content"}, {"path", "content", "source", "executable"})
        or not isinstance(context.get("path"), str)
        or not isinstance(context.get("content"), str)
    ):
        fail("snapshot content context is invalid")
    if "source" in context and (
        context.get("source") not in {"untracked", "worktree"}
        or not isinstance(context.get("executable"), bool)
    ):
        fail("snapshot content context source is invalid")
    context_paths.append(str(context["path"]))
if len(context_paths) != len(set(context_paths)) or set(context_paths) != content_paths:
    fail("snapshot content contexts do not match content coverage")

tree_keys = {
    "path",
    "manifest_path",
    "manifest_bytes",
    "manifest_sha256",
    "file_count",
    "total_bytes",
}
roots: list[str] = []
manifests: set[str] = set()
for tree in trees:
    if not isinstance(tree, dict) or set(tree) != tree_keys:
        fail("generated-tree summary is invalid")
    root = tree.get("path")
    manifest_path = tree.get("manifest_path")
    if (
        not safe_path(root)
        or not isinstance(root, str)
        or manifest_path != f"{root}/manifest.json"
        or manifest_path in manifests
        or not nonnegative(tree.get("manifest_bytes"))
        or not isinstance(tree.get("manifest_sha256"), str)
        or SNAPSHOT_ID.fullmatch(str(tree.get("manifest_sha256"))) is None
        or not nonnegative(tree.get("file_count"))
        or not nonnegative(tree.get("total_bytes"))
        or any(
            PurePosixPath(root).is_relative_to(PurePosixPath(existing))
            or PurePosixPath(existing).is_relative_to(PurePosixPath(root))
            for existing in roots
        )
    ):
        fail("generated-tree summary is invalid")
    roots.append(root)
    assert isinstance(manifest_path, str)
    manifests.add(manifest_path)
    manifest_records = [
        record
        for record in records
        if record.get("coverage") == "content" and record.get("path") == manifest_path
    ]
    members = generated_by_manifest.get(manifest_path, [])
    if (
        len(manifest_records) != 1
        or manifest_records[0].get("bytes") != tree.get("manifest_bytes")
        or manifest_records[0].get("sha256") != tree.get("manifest_sha256")
        or len(members) != tree.get("file_count")
        or sum(int(record["bytes"]) for record in members) != tree.get("total_bytes")
        or any(
            not PurePosixPath(str(record["path"])).is_relative_to(PurePosixPath(root))
            for record in members
        )
    ):
        fail("generated-tree coverage is invalid")
if roots != sorted(roots) or set(generated_by_manifest) != manifests:
    fail("generated-tree declarations are ambiguous")

for record in records:
    relative = str(record["path"])
    expected_size = int(record["bytes"])
    if source == "worktree":
        observed = read_worktree(relative, expected_size)
    else:
        observed = read_git_version(relative, expected_size, source)
    if observed != (expected_size, record["sha256"], record["executable"]):
        fail(f"workspace drift detected for {relative!r}")

print(
    f"snapshot evidence: passed id={snapshot_id} source={source} "
    f"covered={covered_count}/{changed_count}"
)
