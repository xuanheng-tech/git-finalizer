"""Sensitive-content, token, private-key, and unified binary scanning for Git Finalizer."""

from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any

from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import clean_git_env, is_binary_bytes, run_git
from git_finalizer.json_io import canonical_json, sha256_bytes

MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_LITERAL_BYTES = 1024 * 1024
LITERAL = re.compile(rb'"(?:[^"\\]|\\.)*"')
DIRECTORY_ENV_KEY = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_CREDENTIALS")
SYSTEMD_DIRECTORY = re.compile(r"/run/credentials/[A-Za-z0-9][A-Za-z0-9_.@-]*\.service")
PRIVATE_KEY_END = re.compile(rb"-----END [^\r\n]*-----")
VERDICT_ORDER = ("PRIVATE", "KNOWN", "SSH", "ASSIGNMENT")

# Case-sensitive token signatures avoid uppercase false positives like SK-2026-... or TASK-...
KNOWN_TOKEN_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_-])"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"(?<![A-Za-z0-9_])sk-(?:proj-|ant-[a-z0-9-]+-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9_])whsec_[0-9A-Za-z]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9_])xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(rb"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9_])glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9_])pypi-[A-Za-z0-9_-]{32,}"),
    re.compile(rb"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{20,}"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        rb"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://"
        rb"[^:\s/@\"'<>`]+:[^@\s/\"'<>`]{4,}@[A-Za-z0-9._:-]+",
        re.IGNORECASE,
    ),
)

HEADER_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("PRIVATE", re.compile(rb"-----BEGIN ([A-Z0-9]+[\x09-\x0d ]+)*PRIVATE KEY-----", re.I | re.M)),
    (
        "SSH",
        re.compile(
            rb"(^|[\x09-\x0d ])(?:ssh-(?:rsa|dss|ecdsa|ed25519)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com)[\x09-\x0d ]+[A-Za-z0-9+/=]{24,}",
            re.I | re.M,
        ),
    ),
)

_KEY_ALT = (
    rb"api[_-]?key|secret(?:[_-]?key)?|token|password|passwd|credentials?|"
    rb"private[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|"
    rb"refresh[_-]?token|signing[_-]?key|db[_-]?password"
)

QUOTED_ASSIGNMENT = re.compile(
    rb"(?<![A-Za-z0-9_])['\"]?(?:[A-Za-z0-9_]*_)?(?:" + _KEY_ALT + rb")(?:_[A-Za-z0-9]+)*['\"]?"
    rb"[\x09-\x0d ]*[:=][\x09-\x0d ]*(?:[bfrBuFrR]{1,2})?(?P<q>['\"`])(?P<val>(?:\\.|(?!(?P=q))[^\r\n]){12,})(?P=q)",
    re.I,
)

UNQUOTED_ASSIGNMENT = re.compile(
    rb"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_]*_)?(?:" + _KEY_ALT + rb")(?:_[A-Za-z0-9]+)*"
    rb"[ \t]*(?:=(?![=>])|:[ \t]+)(?P<val>[^\s'\"`#;,{}()\[\]]{12,})",
    re.I,
)

VERDICT_DETECTOR = {
    "PRIVATE": "private-key-header-v1",
    "SSH": "ssh-key",
    "KNOWN": "known-token-v1",
    "ASSIGNMENT": "credential-assignment-v1",
    "ERROR": "content-scan-error",
}


def mask_systemd_directory_references(raw: bytes) -> bytes:
    """Classify narrow static Python credential directory references, without reading them."""
    if b"/run/credentials/" not in raw:
        return raw
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        return raw
    # A bare JSON-like expression is not a Python environment declaration.
    declarations = (
        ast.Assign,
        ast.AnnAssign,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Import,
        ast.ImportFrom,
    )
    if not any(isinstance(node, declarations) for node in tree.body):
        return raw
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def literal_span(node: ast.AST, text: str) -> tuple[int, int] | None:
        if node.lineno != node.end_lineno or node.lineno is None or node.col_offset is None or node.end_col_offset is None:
            return None
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        value = text.encode("ascii")
        if raw[start:end] not in (b'"' + value + b'"', b"'" + value + b"'"):
            return None
        return start, end

    def directory_span(node: ast.AST) -> tuple[int, int] | None:
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SYSTEMD_DIRECTORY.fullmatch(node.value)
            and len(node.value.removeprefix("/run/credentials/")) <= 255
        ):
            return literal_span(node, node.value)
        return None

    spans: list[tuple[int, int]] = []
    for mapping in ast.walk(tree):
        if not isinstance(mapping, ast.Dict):
            continue
        if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in mapping.keys):
            continue
        keys = [key.value for key in mapping.keys]  # type: ignore[union-attr]
        if len(keys) != len(set(keys)):
            continue
        for key, value in zip(mapping.keys, mapping.values, strict=True):
            assert isinstance(key, ast.Constant) and isinstance(key.value, str)
            if not (DIRECTORY_ENV_KEY.fullmatch(key.value) or key.value == "container_credentials"):
                continue
            span = directory_span(value)
            if literal_span(key, key.value) and span:
                spans.append(span)
    for declaration in tree.body:
        if (
            isinstance(declaration, ast.Assign)
            and len(declaration.targets) == 1
            and isinstance(declaration.targets[0], ast.Name)
            and declaration.targets[0].id == "CREDENTIALS"
        ):
            span = directory_span(declaration.value)
            if span:
                spans.append(span)
    for start, end in sorted(spans, reverse=True):
        # Preserve byte positions and never mask adjacent comments or literals.
        raw = raw[:start] + b'""' + b" " * (end - start - 2) + raw[end:]
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> Any:
    raise ValueError("non-JSON constant")


def _json_strings(value: Any):
    if isinstance(value, str):
        yield value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key).encode("utf-8")
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _decoded_assignments(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and len(item.encode("utf-8")) >= 12 and QUOTED_ASSIGNMENT.search(
                str(key).encode("utf-8") + b'="AAAAAAAAAAAA"'
            ):
                yield str(key).encode("utf-8") + b"=" + item.encode("utf-8")
            yield from _decoded_assignments(item)
    elif isinstance(value, list):
        for item in value:
            yield from _decoded_assignments(item)
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        yield from (match.group() for match in QUOTED_ASSIGNMENT.finditer(encoded))


def _match_identity(verdict: str, source: bytes, match: re.Match[bytes]) -> bytes:
    if verdict == "PRIVATE":
        end = PRIVATE_KEY_END.search(source, match.end())
        if end is None:
            line_end = source.find(b"\n", match.end())
            return source[match.start() : line_end if line_end >= 0 else len(source)]
        return source[match.start() : end.end()]
    return match.group()


def _finding_identities(raw: bytes) -> list[tuple[str, bytes]]:
    """Return every `(verdict, identity_bytes)` match for exact baseline subtraction."""
    if len(raw) > MAX_SCAN_BYTES:
        raise ValueError(f"content exceeds scan byte budget ({MAX_SCAN_BYTES} bytes)")
    if not raw or is_binary_bytes(raw):
        return []

    found: list[tuple[str, bytes]] = []
    document: Any = None
    valid_json = False
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        decoded = list(_json_strings(document))
        valid_json = True
    except (ValueError, UnicodeError):
        decoded = []
        for match in LITERAL.finditer(raw):
            if len(match.group()) > MAX_LITERAL_BYTES:
                continue
            try:
                decoded.append(json.loads(match.group()).encode("utf-8"))
            except (ValueError, UnicodeError):
                continue

    for value in (raw, *decoded):
        for verdict, pattern in HEADER_PATTERNS:
            found.extend((verdict, _match_identity(verdict, value, match)) for match in pattern.finditer(value))
        for pattern in KNOWN_TOKEN_PATTERNS:
            found.extend(("KNOWN", _match_identity("KNOWN", value, match)) for match in pattern.finditer(value))

    masked = raw if valid_json else mask_systemd_directory_references(raw)

    assignments = [match.group() for match in QUOTED_ASSIGNMENT.finditer(masked)]
    assignments.extend(match.group() for match in UNQUOTED_ASSIGNMENT.finditer(masked))
    if valid_json:
        assignments.extend(_decoded_assignments(document))
    else:
        def decode_match(m: re.Match[bytes]) -> bytes:
            if len(m.group()) > MAX_LITERAL_BYTES:
                return m.group()
            try:
                return b'"' + json.loads(m.group()).encode("utf-8") + b'"'
            except (ValueError, UnicodeError):
                return m.group()

        decoded_text = LITERAL.sub(decode_match, masked)
        assignments.extend(match.group() for match in QUOTED_ASSIGNMENT.finditer(decoded_text))
        assignments.extend(match.group() for match in UNQUOTED_ASSIGNMENT.finditer(decoded_text))
        for value in decoded:
            assignments.extend(match.group() for match in QUOTED_ASSIGNMENT.finditer(value))
            assignments.extend(match.group() for match in UNQUOTED_ASSIGNMENT.finditer(value))
    found.extend(("ASSIGNMENT", identity) for identity in assignments)
    return found


def findings(raw: bytes) -> dict[str, int]:
    """Count security findings by verdict for callers inspecting category totals."""
    counts: dict[str, int] = {}
    for verdict, _identity in _finding_identities(raw):
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def scan(raw: bytes, *, baseline: bytes | None = None) -> str:
    """Return highest-severity finding verdict (`CLEAR`, `PRIVATE`, `SSH`, `KNOWN`, `ASSIGNMENT`)."""
    remaining = Counter(_finding_identities(raw))
    if baseline is not None:
        remaining -= Counter(_finding_identities(baseline))
    verdicts = {verdict for (verdict, _id), count in remaining.items() if count > 0}
    return next((verdict for verdict in VERDICT_ORDER if verdict in verdicts), "CLEAR")


def scan_diagnostic(
    raw: bytes,
    *,
    path: str,
    stage: str,
    blob_oid: str | None = None,
    baseline: bytes | None = None,
) -> dict[str, Any] | None:
    """Scan bytes against optional baseline and return structured redacted refusal diagnostic if blocked."""
    try:
        verdict = scan(raw, baseline=baseline)
    except ValueError:
        verdict = "ERROR"
    if verdict == "CLEAR":
        return None
    if verdict not in VERDICT_DETECTOR:
        verdict = "ERROR"
    return {
        "path": path,
        "stage": stage,
        "verdict": verdict,
        "detector": VERDICT_DETECTOR[verdict],
        "byte_length": len(raw),
        "binary": is_binary_bytes(raw),
        "sha256": sha256_bytes(raw),
        "blob_oid": blob_oid,
    }


def hash_candidate_clean(
    repo: Path,
    relative_path: str,
    file_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, bytes]:
    """Hash candidate worktree file through Git clean filter inside an isolated temporary object dir."""
    posix = PurePosixPath(relative_path)
    if posix.is_absolute() or ".." in posix.parts:
        raise FinalizerError(ErrorCode.SCOPE_INVALID, f"非法路径: {relative_path}")
    with tempfile.TemporaryDirectory(prefix="gf-scan-cand-") as temporary:
        obj_dir = Path(temporary) / "objects"
        obj_dir.mkdir(mode=0o700)
        scan_env = clean_git_env(env)
        scan_env["GIT_OBJECT_DIRECTORY"] = str(obj_dir)
        hashed = run_git(
            repo,
            "hash-object",
            "-w",
            "--path",
            relative_path,
            "--",
            str(file_path),
            env=scan_env,
        )
        if hashed.returncode != 0:
            raise FinalizerError(
                ErrorCode.CONTENT_SCAN_BLOCKED,
                f"候选文件 Git clean-filter 处理失败: {relative_path}",
                final_phase="local_validation",
            )
        oid = hashed.stdout.decode("ascii").strip()
        size_res = run_git(repo, "cat-file", "-s", oid, env=scan_env)
        if size_res.returncode != 0:
            raise FinalizerError(
                ErrorCode.CONTENT_SCAN_BLOCKED,
                f"无法读取候选对象大小: {relative_path}",
                final_phase="local_validation",
            )
        size = int(size_res.stdout.decode("ascii").strip())
        if size > MAX_SCAN_BYTES:
            raise ValueError(f"candidate exceeds scan byte budget ({size} > {MAX_SCAN_BYTES})")
        blob = run_git(repo, "cat-file", "blob", oid, env=scan_env)
        if blob.returncode != 0:
            raise FinalizerError(
                ErrorCode.CONTENT_SCAN_BLOCKED,
                f"无法读取候选对象内容: {relative_path}",
                final_phase="local_validation",
            )
        return oid, blob.stdout


def scan_changelog_header_preservation(
    raw: bytes,
    baseline_raw: bytes,
) -> bool:
    """Allow CHANGELOG.md ONLY when inline-backtick header examples match baseline byte-for-byte."""
    if baseline_raw == raw:
        return True
    baseline_text = baseline_raw.decode("utf-8", errors="strict")
    current_text = raw.decode("utf-8", errors="strict")
    header_re = re.compile(r"`-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----`", re.IGNORECASE)
    end_re = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
    if end_re.search(current_text):
        return False
    base_matches = list(header_re.finditer(baseline_text))
    curr_matches = list(header_re.finditer(current_text))
    if not base_matches or len(base_matches) != len(curr_matches):
        return False
    earliest_base = base_matches[0].start()
    earliest_curr = curr_matches[0].start()
    # Every character from the earliest baseline inline-backtick header to EOF must remain byte-for-byte identical.
    if current_text[earliest_curr:] != baseline_text[earliest_base:]:
        return False
    # Prefix before earliest historical marker must have no private headers/secrets.
    prefix_bytes = current_text[:earliest_curr].encode("utf-8")
    return scan(prefix_bytes) == "CLEAR"


def scan_published_prose_reasons(
    raw: bytes,
    baseline_raw: bytes,
) -> bool:
    """Allow `*_REASONS` module-level dict literals matching baseline raw key/value pairs."""
    if len(raw) > 1024 * 1024 or len(baseline_raw) > 1024 * 1024:
        return False
    try:
        base_text = baseline_raw.decode("utf-8", errors="strict")
        curr_text = raw.decode("utf-8", errors="strict")
        base_tree = ast.parse(base_text)
        curr_tree = ast.parse(curr_text)
    except (UnicodeDecodeError, SyntaxError):
        return False

    _PROSE_PAIR_RE = re.compile(r"[A-Za-z]+(?:[ -]+[A-Za-z]+){2,}")
    _REASONS_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*_REASONS")

    def reason_pairs(tree: ast.Module) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for stmt in tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and _REASONS_NAME_RE.fullmatch(stmt.targets[0].id)
                and isinstance(stmt.value, ast.Dict)
            ):
                for key_node, val_node in zip(stmt.value.keys, stmt.value.values, strict=True):
                    if (
                        isinstance(key_node, ast.Constant)
                        and isinstance(val_node, ast.Constant)
                        and isinstance(key_node.value, str)
                        and isinstance(val_node.value, str)
                        and _PROSE_PAIR_RE.fullmatch(key_node.value)
                        and _PROSE_PAIR_RE.fullmatch(val_node.value)
                    ):
                        pairs.add((key_node.value, val_node.value))
        return pairs

    base_pairs = reason_pairs(base_tree)
    curr_pairs = reason_pairs(curr_tree)
    if not base_pairs or not curr_pairs <= base_pairs:
        return False

    # Build sanitized copy of curr_text where matched published reason dict entries are blanked.
    lines = curr_text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def char_span(node: ast.AST) -> tuple[int, int] | None:
        if node.lineno is None or node.end_lineno is None or node.col_offset is None or node.end_col_offset is None:
            return None
        return offsets[node.lineno - 1] + node.col_offset, offsets[node.end_lineno - 1] + node.end_col_offset

    spans_to_mask: list[tuple[int, int]] = []
    seen_pairs: dict[tuple[str, str], int] = {}
    base_counts: dict[tuple[str, str], int] = {}
    for stmt in base_tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and _REASONS_NAME_RE.fullmatch(stmt.targets[0].id)
            and isinstance(stmt.value, ast.Dict)
        ):
            for k, v in zip(stmt.value.keys, stmt.value.values, strict=True):
                if (
                    isinstance(k, ast.Constant)
                    and isinstance(v, ast.Constant)
                    and isinstance(k.value, str)
                    and isinstance(v.value, str)
                    and _PROSE_PAIR_RE.fullmatch(k.value)
                    and _PROSE_PAIR_RE.fullmatch(v.value)
                ):
                    pair = (k.value, v.value)
                    base_counts[pair] = base_counts.get(pair, 0) + 1

    for stmt in curr_tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and _REASONS_NAME_RE.fullmatch(stmt.targets[0].id)
            and isinstance(stmt.value, ast.Dict)
        ):
            for k, v in zip(stmt.value.keys, stmt.value.values, strict=True):
                if not (
                    isinstance(k, ast.Constant)
                    and isinstance(v, ast.Constant)
                    and isinstance(k.value, str)
                    and isinstance(v.value, str)
                    and _PROSE_PAIR_RE.fullmatch(k.value)
                    and _PROSE_PAIR_RE.fullmatch(v.value)
                ):
                    continue
                pair = (k.value, v.value)
                seen_pairs[pair] = seen_pairs.get(pair, 0) + 1
                if seen_pairs[pair] > base_counts.get(pair, 0):
                    return False
                s_k = char_span(k)
                s_v = char_span(v)
                if not s_k or not s_v:
                    return False
                between = curr_text[s_k[1] : s_v[0]]
                if "#" in between or any(c not in " \t\r\n:" for c in between):
                    return False
                spans_to_mask.append((s_k[0], s_v[1]))

    if not spans_to_mask:
        return False

    buf = list(curr_text)
    for start, end in spans_to_mask:
        for idx in range(start, end):
            if buf[idx] not in ("\n", "\r"):
                buf[idx] = " "
    masked = "".join(buf).encode("utf-8")
    return scan(masked) == "CLEAR"


def canonical_report_line(diagnostic: dict[str, Any] | None) -> str:
    if diagnostic is None:
        return "CLEAR"
    return f"{diagnostic['verdict']}"


def format_content_scan_summary(diagnostic: dict[str, Any] | None) -> bytes:
    return canonical_json(diagnostic)
