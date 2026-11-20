"""Portable Git invocation, validation, binary detection, and isolated index helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from git_finalizer.errors import ErrorCode, FinalizerError

FULL_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SAFE_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,127}$")
DEFAULT_LOCAL_GIT_TIMEOUT = 120.0
DEFAULT_REMOTE_GIT_TIMEOUT = 120.0
BINARY_CHUNK_SIZE = 1024 * 1024


def remote_git_timeout() -> float:
    """Resolve remote git timeout, allowing bounded positive GF_REMOTE_GIT_TIMEOUT override."""
    raw = os.environ.get("GF_REMOTE_GIT_TIMEOUT", "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            return DEFAULT_REMOTE_GIT_TIMEOUT
        if 0.0 < val <= 300.0:
            return val
    return DEFAULT_REMOTE_GIT_TIMEOUT


def find_git(path_env: str | None = None) -> str:
    """Resolve the git executable portably via PATH."""
    resolved = shutil.which("git", path=path_env) if path_env is not None else shutil.which("git")
    if not resolved:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            "PATH 中未找到可用的 git 可执行文件",
            final_phase="cli",
        )
    return resolved


def clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a clean deterministic Git environment preserving caller PATH/HOME/TMPDIR."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if extra:
        env.update(extra)
    return env


def validate_oid(value: str, label: str = "OID") -> str:
    if FULL_OID_RE.fullmatch(value) is None:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            f"{label} 必须是完整小写十六进制 OID (40 或 64 位)",
            final_phase="cli",
        )
    return value


def validate_remote_name(remote: str) -> str:
    if (
        not remote
        or remote.startswith("-")
        or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in remote)
        or ".." in remote
        or "\\" in remote
        or "\0" in remote
        or SAFE_REMOTE_RE.fullmatch(remote) is None
    ):
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            f"非法的 remote 名称: {remote!r}",
            final_phase="cli",
        )
    return remote


def validate_branch_name(branch: str) -> str:
    if (
        not branch
        or branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
        or branch.startswith("refs/")
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or any(ch in branch for ch in (" ", "\t", "\n", "\r", "\0", "~", "^", ":", "?", "*", "[", "\\"))
    ):
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            f"非法的 branch 名称: {branch!r}",
            final_phase="cli",
        )
    return branch


def run_git(
    repo: Path | None,
    *arguments: str,
    timeout: float = DEFAULT_LOCAL_GIT_TIMEOUT,
    check: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    input_data: bytes | str | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run git safely with clean env, close_fds=True, and explicit timeouts."""
    resolved_git = find_git(env.get("PATH") if env else None)
    command = [resolved_git]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(arguments)
    run_env = clean_git_env(env) if env is None else {
        **env,
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        completed = subprocess.run(
            command,
            input=input_data,
            capture_output=True,
            text=text,
            timeout=timeout,
            env=run_env,
            close_fds=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FinalizerError(
            ErrorCode.REMOTE_UNAVAILABLE if "ls-remote" in arguments or "fetch" in arguments or "push" in arguments else ErrorCode.INTERNAL_ERROR,
            f"git 命令执行超时 ({arguments[0] if arguments else 'git'})",
            status="failed",
        ) from exc
    except OSError as exc:
        raise FinalizerError(
            ErrorCode.INTERNAL_ERROR,
            f"无法执行 git 命令: {exc}",
            status="failed",
        ) from exc
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, command, output=completed.stdout, stderr=completed.stderr
        )
    return completed


def parse_nul_records(raw: bytes) -> list[str]:
    """Decode strict UTF-8 NUL-delimited Git records, preserving empty middle elements."""
    if not raw:
        return []
    if not raw.endswith(b"\0"):
        raise ValueError("unterminated NUL record stream")
    parts = raw.split(b"\0")
    parts.pop()
    return [part.decode("utf-8", errors="strict") for part in parts]


def is_binary_bytes(data: bytes) -> bool:
    """Single canonical binary check across worktree, candidate, index, and history."""
    return b"\x00" in data


def inspect_file_stream(path: Path, max_bytes: int | None = None) -> tuple[bool, int]:
    """Stream-inspect a regular file for NUL bytes and byte size."""
    binary = False
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(BINARY_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"file exceeds scan byte budget ({max_bytes} bytes)")
            if not binary and b"\x00" in chunk:
                binary = True
    return binary, total


def read_blob_size(repo: Path, oid: str, *, env: dict[str, str] | None = None) -> int:
    """Check object existence/size cleanly via git cat-file -s before reading blob bytes."""
    completed = run_git(repo, "cat-file", "-s", oid, env=env)
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.INTERNAL_ERROR,
            f"无法读取 Git 对象大小: {oid}",
            status="failed",
        )
    return int(completed.stdout.decode("ascii").strip())


def read_blob_bytes(
    repo: Path,
    oid: str,
    *,
    max_bytes: int,
    env: dict[str, str] | None = None,
) -> bytes:
    """Verify blob byte size BEFORE materializing the blob in memory."""
    size = read_blob_size(repo, oid, env=env)
    if size > max_bytes:
        raise ValueError(f"Git blob {oid} exceeds scan byte budget ({size} > {max_bytes})")
    completed = run_git(repo, "cat-file", "blob", oid, env=env)
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.INTERNAL_ERROR,
            f"无法读取 Git blob 内容: {oid}",
            status="failed",
        )
    return completed.stdout


def read_baseline_blob(
    repo: Path,
    ref_expr: str,
    path: str,
    *,
    max_bytes: int,
    env: dict[str, str] | None = None,
) -> tuple[str | None, bytes | None]:
    """Read `path` blob oid and bytes from `ref_expr` if present as a regular blob."""
    clean_env = clean_git_env(env)
    ls = run_git(repo, "--literal-pathspecs", "ls-tree", "-z", ref_expr, "--", path, env=clean_env)
    if ls.returncode != 0 or not ls.stdout:
        return None, None
    entries = parse_nul_records(ls.stdout)
    if len(entries) != 1:
        return None, None
    meta, _, _ = entries[0].partition("\t")
    parts = meta.split()
    if len(parts) != 3 or parts[1] != "blob":
        return None, None
    oid = parts[2]
    return oid, read_blob_bytes(repo, oid, max_bytes=max_bytes, env=clean_env)


@contextmanager
def isolated_staging_env(
    repo: Path,
    git_dir: Path,
    common_dir: Path,
    *,
    base_env: dict[str, str] | None = None,
) -> Iterator[tuple[dict[str, str], Path]]:
    """Build an isolated temporary GIT_INDEX_FILE + temp object store without touching .git/index or .git/objects."""
    with tempfile.TemporaryDirectory(prefix="gf-isolated-stage-") as temporary:
        tmp_path = Path(temporary)
        temp_index = tmp_path / "index"
        real_index = git_dir / "index"
        if real_index.exists():
            if real_index.is_symlink() or not real_index.is_file():
                raise FinalizerError(
                    ErrorCode.REPO_INVALID,
                    "拒绝非普通文件类型的 Git index",
                    final_phase="preflight",
                )
            shutil.copy2(real_index, temp_index)
        temp_objects = tmp_path / "objects"
        temp_objects.mkdir(mode=0o700)
        alt_objects = common_dir / "objects"
        existing_alt = (base_env or os.environ).get("GIT_ALTERNATE_OBJECT_DIRECTORIES", "")
        alts = os.pathsep.join(part for part in (str(alt_objects), existing_alt) if part)
        env = clean_git_env(base_env)
        env["GIT_INDEX_FILE"] = str(temp_index)
        env["GIT_OBJECT_DIRECTORY"] = str(temp_objects)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = alts
        yield env, temp_index
