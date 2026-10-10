#!/usr/bin/env python3
"""Scan raw and decoded content; isolate candidate clean-filter objects from Git state."""

from __future__ import annotations

import ast
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
    ("known-token-v1", rb"(AKIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|(?<![A-Za-z0-9_])sk-(proj-)?[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})", "KNOWN"),
    ("ssh-key", rb"(^|[\x09-\x0d ])ssh-(rsa|dss|ecdsa|ed25519)[\x09-\x0d ]+[A-Za-z0-9+/]{40,}", "SSH"),
)
ASSIGNMENT = re.compile(
    rb"(credentials?|password|passwd|secret|token|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)['\"]?[\x09-\x0d ]*[:=][\x09-\x0d ]*['\"][^'\"\r\n]{12,}['\"]",
    re.I,
)
LITERAL = re.compile(rb'"(?:[^"\\]|\\.)*"')
DIRECTORY_ENV_KEY = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_CREDENTIALS")
SYSTEMD_DIRECTORY = re.compile(r"/run/credentials/[A-Za-z0-9][A-Za-z0-9_.@-]*\.service")


def mask_systemd_directory_references(raw: bytes) -> bytes:
    """Classify narrow static Python credential directory references, without reading them."""
    if b"/run/credentials/" not in raw:
        return raw
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        return raw
    # A bare JSON-like expression is not a Python environment declaration.
    declarations = (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef,
                    ast.ClassDef, ast.Import, ast.ImportFrom)
    if not any(isinstance(node, declarations) for node in tree.body):
        return raw
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def literal_span(node, text):
        if node.lineno != node.end_lineno:
            return None
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        value = text.encode("ascii")
        if raw[start:end] not in (b'"' + value + b'"', b"'" + value + b"'"):
            return None
        return start, end

    def directory_span(node):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and SYSTEMD_DIRECTORY.fullmatch(node.value)
                and len(node.value.removeprefix("/run/credentials/")) <= 255):
            return literal_span(node, node.value)
        return None

    spans = []
    for mapping in ast.walk(tree):
        if not isinstance(mapping, ast.Dict):
            continue
        if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str)
               for key in mapping.keys):
            continue
        keys = [key.value for key in mapping.keys]
        if len(keys) != len(set(keys)):
            continue
        for key, value in zip(mapping.keys, mapping.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and (DIRECTORY_ENV_KEY.fullmatch(key.value)
                         or key.value == "container_credentials")):
                continue
            span = directory_span(value)
            if literal_span(key, key.value) and span:
                spans.append(span)
    for declaration in tree.body:
        if (isinstance(declaration, ast.Assign) and len(declaration.targets) == 1
                and isinstance(declaration.targets[0], ast.Name)
                and declaration.targets[0].id == "CREDENTIALS"):
            span = directory_span(declaration.value)
            if span:
                spans.append(span)
    for start, end in sorted(spans, reverse=True):
        # Preserve byte positions and never mask adjacent comments or literals.
        raw = raw[:start] + b'""' + b" " * (end - start - 2) + raw[end:]
    return raw


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
    masked = raw if valid_json else mask_systemd_directory_references(raw)
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
    decoded_text = LITERAL.sub(decode_match, masked)
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
        report = kind.startswith("report-")
        if report:
            kind = kind.removeprefix("report-")
        oid = None
        if kind == "candidate":
            raw, oid = candidate(repo, source)
        else:
            if kind == "file":
                raw = Path(source).read_bytes()
            elif kind == "blob":
                raw = git(repo, "cat-file", "blob", source)
                oid = source
            else:
                raise ValueError("unknown scan kind")
        verdict = scan(raw, excepted, only)
        if report:
            # Bind metadata to these same bytes, never a second scan or source read.
            print(verdict, len(raw), int(b"\0" in raw), hashlib.sha256(raw).hexdigest(),
                  oid or "-", hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        elif kind == "candidate":
            print(verdict, len(raw), int(b"\0" in raw), hashlib.sha256(raw).hexdigest(), oid)
        else:
            print(verdict)
        return 0
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        # Never expose source bytes, filter diagnostics or exception values.
        print("ERROR")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
