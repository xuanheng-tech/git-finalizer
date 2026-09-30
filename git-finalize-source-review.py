#!/usr/bin/env python3
"""Validate explicit, external exact-source reviews; never exempt content scanning."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


class ReviewError(Exception):
    """A reviewed-source identity is absent, stale or inconsistent."""


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ReviewError(reason)


def object_fields(value: object, fields: set[str]) -> dict[str, object]:
    require(isinstance(value, dict) and set(value) == fields, "invalid review fields")
    assert isinstance(value, dict)
    return value


def text(value: object) -> str:
    require(
        isinstance(value, str)
        and 0 < len(value.strip()) <= 1024
        and not any(c in value for c in "\x00\n\r\t"),
        "invalid review text",
    )
    assert isinstance(value, str)
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def external_file(value: object, repo: Path, common: Path) -> Path:
    path = Path(text(value))
    require(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and path.is_file()
        and not path.is_symlink()
        and not path.is_relative_to(repo)
        and not path.is_relative_to(common),
        "review and evidence must be external canonical regular files",
    )
    info = path.stat()
    require(
        info.st_uid == os.geteuid() and info.st_mode & 0o022 == 0,
        "review or evidence ownership/mode mismatch",
    )
    require(info.st_size <= 65536, "review or evidence exceeds 64 KiB")
    return path


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, "duplicate review JSON key")
        result[key] = value
    return result


def reject_json_constant(_value: str) -> None:
    raise ReviewError("non-finite JSON constant")


def validate_source(content: bytes, suffix: str) -> None:
    source_text = content.decode("utf-8")
    require("\x00" not in source_text, "review source contains NUL")
    if suffix == ".py":
        ast.parse(source_text)
    elif suffix == ".json":
        json.loads(
            source_text,
            object_pairs_hook=unique_object,
            parse_constant=reject_json_constant,
        )
    # CSS is bounded UTF-8 text. Project checks own its grammar and semantics;
    # an exact path review never replaces the mandatory secret-content scans.


def git(repo: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", "--no-optional-locks", "-C", str(repo), *arguments],
        stderr=subprocess.DEVNULL,
        timeout=15,
    )


def validate(args: argparse.Namespace) -> list[dict[str, object]]:
    repo = Path(args.repo).resolve(strict=True)
    common = Path(git(repo, "rev-parse", "--git-common-dir").decode().strip())
    if not common.is_absolute():
        common = repo / common
    common = common.resolve(strict=True)
    review_file = external_file(args.review, repo, common)
    raw = review_file.read_bytes()
    review = object_fields(
        json.loads(raw, object_pairs_hook=unique_object),
        {
            "schema_version",
            "kind",
            "repository",
            "allocation",
            "scope_sha256",
            "reviewed_at",
            "expires_at",
            "reviews",
        },
    )
    require(
        type(review["schema_version"]) is int and review["schema_version"] == 1,
        "unknown review schema",
    )
    require(review["kind"] == "reviewed-sensitive-source", "unknown review kind")
    roots = git(repo, "rev-list", "--max-parents=0", "HEAD").decode().splitlines()
    require(
        len(roots) == 1
        and review["repository"]
        == {"common_dir": str(common), "root_commit": roots[0]},
        "review repository mismatch",
    )
    allocation = {
        "repository_id": args.repository_id,
        "allocation_id": args.allocation_id,
        "task_key": args.task_key,
        "authority_key": args.authority_key,
        "worktree_path": str(repo),
    }
    require(
        all(allocation.values()) and review["allocation"] == allocation,
        "review allocation mismatch",
    )
    require(len(set(args.paths)) == len(args.paths), "duplicate scope path")
    for scope_path in args.paths:
        relative_scope = PurePosixPath(text(scope_path))
        require(
            not relative_scope.is_absolute()
            and str(relative_scope) == scope_path
            and relative_scope.parts
            and not any(part in {"..", ".git"} for part in relative_scope.parts)
            and not any(c in scope_path for c in "*?[]\\"),
            "invalid exact scope path",
        )
    require(
        review["scope_sha256"]
        == digest(("\n".join(sorted(args.paths)) + "\n").encode()),
        "review scope mismatch",
    )
    reviewed_at = datetime.fromisoformat(text(review["reviewed_at"]))
    expires_at = datetime.fromisoformat(text(review["expires_at"]))
    now = datetime.now(timezone.utc)
    require(
        reviewed_at.utcoffset() == timedelta(0)
        and expires_at.utcoffset() == timedelta(0),
        "review timestamps must be UTC",
    )
    require(
        reviewed_at <= now + timedelta(minutes=5)
        and reviewed_at < expires_at
        and now < expires_at <= reviewed_at + timedelta(days=7),
        "review is stale",
    )
    entries = review["reviews"]
    require(
        isinstance(entries, list) and 1 <= len(entries) <= 16,
        "expected 1..16 source reviews",
    )
    assert isinstance(entries, list)
    seen: set[str] = set()
    receipts: list[dict[str, object]] = []
    for item in entries:
        entry = object_fields(item, {"path", "sha256", "purpose", "evidence"})
        path = text(entry["path"])
        relative = PurePosixPath(path)
        require(
            not relative.is_absolute()
            and str(relative) == path
            and ".." not in relative.parts
            and relative.suffix in {".py", ".css", ".json"}
            and not any(c in path for c in "*?[]\\")
            and path in args.paths
            and path not in seen,
            "invalid exact source path",
        )
        seen.add(path)
        expected = text(entry["sha256"])
        require(re.fullmatch(r"[0-9a-f]{64}", expected), "invalid source digest")
        purpose = text(entry["purpose"])
        evidence = object_fields(entry["evidence"], {"path", "sha256"})
        evidence_file = external_file(evidence["path"], repo, common)
        require(
            digest(evidence_file.read_bytes()) == evidence["sha256"],
            "review evidence drift",
        )
        if args.phase == "worktree":
            source = repo / path
            require(
                source.resolve(strict=True) == source
                and source.is_file()
                and not source.is_symlink(),
                "review source type mismatch",
            )
            require(source.stat().st_size <= 1024 * 1024, "review source exceeds 1 MiB")
            content = source.read_bytes()
        else:
            spec = ":" + path if args.phase == "index" else "HEAD:" + path
            content = git(repo, "show", spec)
            mode = (
                git(repo, "ls-files", "--stage", "--", path).split()[0]
                if args.phase == "index"
                else git(repo, "ls-tree", "HEAD", "--", path).split()[0]
            )
            require(mode in {b"100644", b"100755"}, "review source type mismatch")
        require(
            len(content) <= 1024 * 1024 and digest(content) == expected,
            "review source hash mismatch",
        )
        validate_source(content, relative.suffix)
        receipts.append(
            {
                "review_id": "sha256:" + digest(raw),
                "path": path,
                "sha256": expected,
                "purpose_sha256": digest(purpose.encode()),
                "evidence_sha256": evidence["sha256"],
                "repository": review["repository"],
                "allocation": allocation,
                "scope_sha256": review["scope_sha256"],
                "path_review": "validated",
                "content_scan": "required",
            }
        )
    return receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "review",
        "repo",
        "repository-id",
        "allocation-id",
        "task-key",
        "authority-key",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--phase", choices=("worktree", "index", "head"), required=True)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    try:
        receipts = validate(args)
    except (
        ReviewError,
        OSError,
        ValueError,
        TypeError,
        SyntaxError,
        IndexError,
        subprocess.SubprocessError,
    ):
        # No JSON/source values, exception text or subprocess output are diagnostics.
        print("reviewed-sensitive-source validation blocked", file=sys.stderr)
        return 1
    for receipt in receipts:
        print(str(receipt["path"]) + "\t" + json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
