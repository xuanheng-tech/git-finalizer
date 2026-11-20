"""Exact unified-diff `.patch` single-space empty-context whitespace classifier."""

from __future__ import annotations

import re

from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import clean_git_env, run_git

MAX_PATCH_BYTES = 1_048_576
HUNK_HEADER_RE = re.compile(
    rb"^@@ -(?P<old_start>[0-9]+)(?:,(?P<old_count>[0-9]+))? "
    rb"\+(?P<new_start>[0-9]+)(?:,(?P<new_count>[0-9]+))? @@"
)


def parse_counted_patch_empty_context_lines(raw: bytes) -> tuple[bool, set[int], int]:
    """Verify `.patch` structure and return (valid, empty_context_1_based_lines, line_count)."""
    if len(raw) > MAX_PATCH_BYTES or b"\0" in raw:
        return False, set(), 0
    lines = raw.splitlines(keepends=True)
    if not lines or not lines[-1].endswith(b"\n"):
        return False, set(), 0

    in_hunk = False
    old_left = 0
    new_left = 0
    saw_hunk = False
    empty_context_lines: set[int] = set()

    for lineno, line_with_nl in enumerate(lines, start=1):
        line = line_with_nl[:-1]
        if line.endswith(b"\r"):
            return False, set(), 0
        if in_hunk:
            if line == b"\\ No newline at end of file":
                continue
            if line.startswith(b" "):
                if line == b" ":
                    empty_context_lines.add(lineno)
                elif line.endswith((b" ", b"\t")):
                    return False, set(), 0
                old_left -= 1
                new_left -= 1
            elif line.startswith(b"-"):
                old_left -= 1
            elif line.startswith(b"+"):
                if line.endswith((b" ", b"\t")):
                    return False, set(), 0
                new_left -= 1
            else:
                return False, set(), 0
            if old_left < 0 or new_left < 0:
                return False, set(), 0
            if old_left == 0 and new_left == 0:
                in_hunk = False
            continue

        match = HUNK_HEADER_RE.match(line)
        if match:
            saw_hunk = True
            old_left = int(match.group("old_count") or b"1")
            new_left = int(match.group("new_count") or b"1")
            in_hunk = old_left > 0 or new_left > 0
            continue
        if line.endswith((b" ", b"\t")):
            return False, set(), 0

    if in_hunk or not saw_hunk:
        return False, set(), 0
    return True, empty_context_lines, len(lines)


def allow_patch_context_whitespace_only(
    repo,
    path: str,
    raw: bytes,
    diff_check_output: str,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Return True iff every whitespace diagnostic in `diff_check_output` is a valid counted-hunk empty context space."""
    if not path.endswith(".patch"):
        return False
    valid, allowed_lines, total_lines = parse_counted_patch_empty_context_lines(raw)
    if not valid or not allowed_lines:
        return False

    # Confirm Git itself parses the patch cleanly with apply --numstat without applying it.
    try:
        applied = run_git(
            repo,
            "apply",
            "--numstat",
            "-",
            timeout=15.0,
            env=clean_git_env(env),
            input_data=raw,
        )
    except FinalizerError:
        return False
    if applied.returncode != 0:
        return False

    for raw_diag in diff_check_output.splitlines():
        diag = raw_diag.strip()
        if not diag:
            continue
        # Formats emitted by git diff --check:
        #   <path>:<lineno>: trailing whitespace.
        #   <path>:<lineno>: new blank line at EOF.
        prefix = f"{path}:"
        if not diag.startswith(prefix):
            return False
        rest = diag[len(prefix) :]
        line_str, sep, message = rest.partition(":")
        if not sep or not line_str.isdigit():
            return False
        lineno = int(line_str)
        msg = message.strip()
        if msg == "trailing whitespace.":
            if lineno not in allowed_lines:
                return False
        elif msg == "new blank line at EOF.":
            # Only exact final context line may trigger EOF blank line.
            if lineno not in allowed_lines or lineno != total_lines:
                return False
        else:
            return False
    return True


def check_patch_bytes(
    repo,
    path: str,
    raw: bytes,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Validate whitespace in a standalone `.patch` blob without polluting repository state."""
    valid, allowed_lines, _ = parse_counted_patch_empty_context_lines(raw)
    if not valid:
        # Check if there is any trailing whitespace at all; ordinary non-whitespace lines pass.
        return not any(
            line.rstrip(b"\r\n").endswith((b" ", b"\t")) for line in raw.splitlines()
        )
    if not allowed_lines:
        return True
    try:
        applied = run_git(
            repo,
            "apply",
            "--numstat",
            "-",
            timeout=15.0,
            env=clean_git_env(env),
            input_data=raw,
        )
    except FinalizerError as exc:
        raise FinalizerError(
            ErrorCode.WHITESPACE_VIOLATION,
            f"补丁文件验证失败: {path}",
            final_phase="local_validation",
        ) from exc
    return applied.returncode == 0
