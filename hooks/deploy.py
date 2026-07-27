#!/usr/bin/env python3
"""Check or explicitly install the versioned Git Finalizer Hook files."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Sequence


SOURCE_DIR = Path(__file__).resolve().parent
LIVE_DIR = Path.home() / ".codex" / "hooks"
MANAGED_FILES = {
    "codex_git_finalize_bridge.py": 0o600,
    "test_codex_git_finalize_bridge.py": 0o644,
}


class DeploymentError(Exception):
    """The requested check or installation cannot proceed safely."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_file(source_dir: Path, name: str) -> Path:
    path = source_dir / name
    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"versioned source is not a regular file: {path}")
    return path


def file_states(
    source_dir: Path,
    live_dir: Path,
) -> list[tuple[str, str, str, bool]]:
    states: list[tuple[str, str, str, bool]] = []
    for name in MANAGED_FILES:
        source = source_file(source_dir, name)
        source_digest = sha256(source)
        target = live_dir / name
        if target.is_symlink():
            live_state = "symlink"
            matches = False
        elif not target.exists():
            live_state = "missing"
            matches = False
        elif not target.is_file():
            live_state = "not-regular"
            matches = False
        else:
            live_state = sha256(target)
            matches = live_state == source_digest
        states.append((name, source_digest, live_state, matches))
    return states


def check(source_dir: Path, live_dir: Path) -> bool:
    consistent = True
    for name, source_digest, live_state, matches in file_states(source_dir, live_dir):
        if matches:
            print(f"ok {name} sha256={source_digest}")
        else:
            consistent = False
            print(
                f"drift {name} source_sha256={source_digest} "
                f"live={live_state}"
            )
    return consistent


def existing_mode(target: Path, default_mode: int) -> int:
    if target.is_symlink():
        raise DeploymentError(f"live target is not a regular file: {target}")
    if not target.exists():
        return default_mode
    if not target.is_file():
        raise DeploymentError(f"live target is not a regular file: {target}")
    metadata = target.stat()
    if metadata.st_uid != os.getuid():
        raise DeploymentError(f"live target is not owned by the current user: {target}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o7000 or mode & 0o002 or not mode & 0o400:
        raise DeploymentError(f"live target permissions are not reasonable: {target}")
    return mode


def install_one(source: Path, target: Path, default_mode: int) -> str:
    source_digest = sha256(source)
    mode = existing_mode(target, default_mode)
    if target.is_file() and not target.is_symlink() and sha256(target) == source_digest:
        return f"unchanged {target.name} sha256={source_digest}"

    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()

    live_digest = sha256(target)
    if live_digest != source_digest:
        raise DeploymentError(f"post-install SHA-256 mismatch: {target}")
    return f"installed {target.name} sha256={live_digest} mode={mode:#05o}"


def install(source_dir: Path, live_dir: Path) -> None:
    if live_dir.is_symlink() or not live_dir.is_dir():
        raise DeploymentError(f"live Hook directory is not a regular directory: {live_dir}")
    for name, default_mode in MANAGED_FILES.items():
        source = source_file(source_dir, name)
        print(install_one(source, live_dir / name, default_mode))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check or explicitly install the two managed Git Finalizer Hook files."
    )
    parser.add_argument("action", choices=("check", "install"))
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.action == "install":
            install(arguments.source_dir, arguments.live_dir)
        return 0 if check(arguments.source_dir, arguments.live_dir) else 1
    except (DeploymentError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
