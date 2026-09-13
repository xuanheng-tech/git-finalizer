#!/usr/bin/env python3
"""Verify one complete Snapshot Runner initial-publication artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_META_BYTES = 64 * 1024
MAX_CONTENT_FILES = 128
MAX_GENERATED_FILES = 512
SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
ARTIFACT_NAMES = {"meta.json", "preview.txt", "snapshot.json"}


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


def check_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        fail(f"{description} is unavailable")
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"{description} is not a private real directory")


def read_artifact(path: Path, maximum: int, description: str) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        fail(f"{description} is unavailable")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size <= 0
        or before.st_size > maximum
    ):
        fail(f"{description} is not a bounded private regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail(f"{description} could not be safely opened")
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            fail(f"{description} changed during safe open")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                fail(f"{description} exceeds its hard limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) or total != opened.st_size:
        fail(f"{description} changed while reading")
    return b"".join(chunks)


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

state_raw = os.environ.get("XDG_STATE_HOME")
state = Path(state_raw) if state_raw else Path.home() / ".local" / "state"
if not state.is_absolute() or ".." in state.parts:
    fail("state home is not canonical")
state = Path(os.path.abspath(state))
store = state / "snapshot-runner" / "snapshots"
directory = store / snapshot_id
for path, description in (
    (state, "state home"),
    (state / "snapshot-runner", "private Snapshot Runner state"),
    (store, "snapshot store"),
    (directory, "snapshot directory"),
):
    check_directory(path, description)
try:
    if {entry.name for entry in directory.iterdir()} != ARTIFACT_NAMES:
        fail("snapshot directory contents are not canonical")
except OSError:
    fail("snapshot directory cannot be enumerated")

snapshot_bytes = read_artifact(
    directory / "snapshot.json", MAX_SNAPSHOT_BYTES, "snapshot.json"
)
preview_bytes = read_artifact(
    directory / "preview.txt", MAX_PREVIEW_BYTES, "preview.txt"
)
meta_bytes = read_artifact(directory / "meta.json", MAX_META_BYTES, "meta.json")
if hashlib.sha256(snapshot_bytes).hexdigest() != snapshot_id:
    fail("snapshot content address does not match")
try:
    envelope = json.loads(snapshot_bytes)
    meta = json.loads(meta_bytes)
except (UnicodeDecodeError, json.JSONDecodeError):
    fail("snapshot artifact JSON is invalid")
meta_keys = {
    "schema_version",
    "producer_security_epoch",
    "snapshot_id",
    "task",
    "repository",
    "snapshot_sha256",
    "snapshot_bytes",
    "preview_sha256",
    "preview_bytes",
}
if (
    not isinstance(meta, dict)
    or set(meta) != meta_keys
    or meta.get("schema_version") != 2
    or meta.get("producer_security_epoch") != 4
    or meta.get("snapshot_id") != snapshot_id
    or meta.get("snapshot_sha256") != snapshot_id
    or meta.get("snapshot_bytes") != len(snapshot_bytes)
    or meta.get("preview_sha256") != hashlib.sha256(preview_bytes).hexdigest()
    or meta.get("preview_bytes") != len(preview_bytes)
):
    fail("snapshot metadata does not bind the artifact")

envelope_keys = {
    "schema_version",
    "producer_security_epoch",
    "task",
    "repository",
    "data",
    "truncated",
    "evidence_gaps",
    "redactions",
    "trust_boundary",
    "security_notice",
}
if (
    not isinstance(envelope, dict)
    or set(envelope) != envelope_keys
    or envelope.get("schema_version") != 2
    or envelope.get("producer_security_epoch") != 4
    or envelope.get("task") != "diff-audit"
    or envelope.get("repository") != repo.name
    or meta.get("task") != envelope.get("task")
    or meta.get("repository") != envelope.get("repository")
    or envelope.get("truncated") is not False
    or envelope.get("evidence_gaps") != []
    or not isinstance(envelope.get("redactions"), dict)
    or not isinstance(envelope.get("trust_boundary"), str)
    or not isinstance(envelope.get("security_notice"), str)
):
    fail("snapshot envelope is incomplete or incompatible")
data = envelope.get("data")
data_keys = {
    "status_short",
    "staged_diff",
    "unstaged_diff",
    "file_context",
    "baseline_kind",
    "baseline_oid",
    "initial_publication",
}
if (
    not isinstance(data, dict)
    or set(data) != data_keys
    or not isinstance(data.get("status_short"), str)
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
