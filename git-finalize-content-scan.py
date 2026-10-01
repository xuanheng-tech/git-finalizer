#!/usr/bin/env python3
"""Scan raw and decoded content; isolate candidate clean-filter objects from Git state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


PATTERNS = (
    ("private-key-header-v1", rb"-----BEGIN ([A-Z0-9]+[\x09-\x0d ]+)*PRIVATE KEY-----", "PRIVATE"),
    ("known-token-v1", rb"(AKIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})", "KNOWN"),
    ("ssh-key", rb"(^|[\x09-\x0d ])ssh-(rsa|dss|ecdsa|ed25519)[\x09-\x0d ]+[A-Za-z0-9+/]{40,}", "SSH"),
)
ASSIGNMENT = re.compile(
    rb"(credentials?|password|passwd|secret|token|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)['\"]?[\x09-\x0d ]*[:=][\x09-\x0d ]*['\"][^'\"\r\n]{12,}['\"]",
    re.I,
)
LITERAL = re.compile(rb'"(?:[^"\\]|\\.)*"')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("non-JSON constant")


def strings(value):
    if isinstance(value, str):
        yield value.encode()
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key.encode()
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def decoded_assignment(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and len(item.encode()) >= 12 and ASSIGNMENT.search(
                key.encode() + b'="AAAAAAAAAAAA"'
            ):
                return True
            if decoded_assignment(item):
                return True
    elif isinstance(value, list):
        return any(decoded_assignment(item) for item in value)
    elif isinstance(value, str):
        return ASSIGNMENT.search(value.encode()) is not None
    return False


def scan(raw: bytes, excepted: str = "", only: str = "") -> str:
    document = None
    valid_json = False
    try:
        document = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        decoded = list(strings(document))
        valid_json = True
    except (ValueError, UnicodeError):
        # A bad container must not hide signatures in otherwise valid literals.
        # Keep every occurrence, including values discarded by duplicate keys.
        decoded = []
        for match in LITERAL.finditer(raw):
            try:
                decoded.append(json.loads(match.group()).encode())
            except (ValueError, UnicodeError):
                continue
    for name, pattern, verdict in PATTERNS:
        if (not only or only == name) and "|" + name + "|" not in excepted:
            detector = re.compile(pattern, re.I | re.M)
            if any(detector.search(value) for value in (raw, *decoded)):
                return verdict
    if (only and only != "credential-assignment-v1") or "|credential-assignment-v1|" in excepted:
        return "CLEAR"

    retirement = document.get("branch_retirement_contract") if isinstance(document, dict) else None
    declaration = retirement.get("execution_authorization") if isinstance(retirement, dict) else None
    qualified_schema = (
        valid_json and isinstance(document, dict)
        and type(document.get("schema_version")) is int and document["schema_version"] == 1
        and document.get("tool_name") == "worktree-controller"
        and type(document.get("contract_version")) is int and document["contract_version"] > 0
        and type(document.get("branch_retirement_execution_version")) is int
        and document["branch_retirement_execution_version"] == 1
        and isinstance(retirement, dict) and retirement.get("contract") == "branch-retirement/v3"
        and isinstance(declaration, dict)
        and declaration.get("contract") == "branch-retirement-execution/v1"
        and declaration.get("capability") == "branch_retirement_execution_version"
    )
    description = (
        "a 256-bit Controller-generated capability returned only by authorize and "
        "compared constant-time at complete or abandon; it is excluded from the "
        "identity so it cannot change the authorization address"
    )
    masked = raw
    if qualified_schema and declaration.get("token") == description:
        descriptions = [match for match in LITERAL.finditer(raw) if json.loads(match.group()) == description]
        if len(descriptions) == 1:
            match = descriptions[0]
            masked = raw[:match.start()] + b'""' + raw[match.end():]
            declaration["token"] = ""
    if ASSIGNMENT.search(masked):
        return "ASSIGNMENT"
    if valid_json:
        return "ASSIGNMENT" if decoded_assignment(document) else "CLEAR"
    # Decode adjacent JSON key/value literals without accepting an invalid
    # document as the narrowly qualified public contract above.
    def decode_match(match):
        try:
            return b'"' + json.loads(match.group()).encode() + b'"'
        except (ValueError, UnicodeError):
            return match.group()
    decoded_text = LITERAL.sub(decode_match, raw)
    return "ASSIGNMENT" if ASSIGNMENT.search(decoded_text) or any(ASSIGNMENT.search(value) for value in decoded) else "CLEAR"


def git(repo: str, *args: str, env=None) -> bytes:
    return subprocess.check_output(
        ["git", "--no-optional-locks", "-C", repo, *args],
        env=env, stderr=subprocess.DEVNULL,
    )


def candidate(repo: str, path: str) -> tuple[bytes, str]:
    # Git performs its own attributes, encoding and clean-filter conversion.
    # Write only into a disposable object store, never the repository's store
    # or index. Removal also covers failure in filters and object reads.
    with tempfile.TemporaryDirectory(prefix="gf-candidate-objects-") as objects:
        environment = dict(os.environ, GIT_OBJECT_DIRECTORY=objects, GIT_OPTIONAL_LOCKS="0")
        oid = git(repo, "hash-object", "-w", "--path=" + path, "--", path, env=environment).decode().strip()
        return git(repo, "cat-file", "blob", oid, env=environment), oid


def main() -> int:
    try:
        kind, source, repo, excepted, only = sys.argv[1:]
        if kind == "candidate":
            raw, oid = candidate(repo, source)
            print(scan(raw, excepted, only), len(raw), int(b"\0" in raw), hashlib.sha256(raw).hexdigest(), oid)
        else:
            if kind == "file":
                raw = Path(source).read_bytes()
            elif kind == "blob":
                raw = git(repo, "cat-file", "blob", source)
            else:
                raise ValueError("unknown scan kind")
            print(scan(raw, excepted, only))
        return 0
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        # Never expose source bytes, filter diagnostics or exception values.
        print("ERROR")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
