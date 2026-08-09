#!/usr/bin/env python3
"""Check or explicitly install the versioned Three-tool Git workflow Skill."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Sequence

import quick_validate


SOURCE_DIR = Path(__file__).resolve().parent
LIVE_DIR = Path.home() / ".agents" / "skills" / "three-tool-git-workflow"
MANAGED_FILES = {
    "SKILL.md": 0o600,
    "quick_validate.py": 0o600,
    "unpublished_queue.py": 0o600,
    "references/context-loader.md": 0o600,
    "references/git-finalizer.md": 0o600,
    "references/snapshot-runner.md": 0o600,
    "references/unpublished-queue.md": 0o600,
}
SOURCE_ONLY_FILES = (
    "README.md",
    "deploy.py",
    "test_deploy.py",
    "test_unpublished_queue.py",
)
EXPECTED_LIVE_DIRS = {"references"}


class DeploymentError(Exception):
    """The requested check or installation cannot proceed safely."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def regular_owned_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"{label} is not a regular file: {path}")
    if path.stat().st_uid != os.getuid():
        raise DeploymentError(f"{label} is not owned by the current user: {path}")


def validate_source(source_dir: Path) -> None:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise DeploymentError(f"versioned Skill source is not a directory: {source_dir}")
    if source_dir.stat().st_uid != os.getuid():
        raise DeploymentError(f"versioned Skill source is not owned by the current user: {source_dir}")
    for name in (*MANAGED_FILES, *SOURCE_ONLY_FILES):
        path = source_dir / name
        regular_owned_file(path, "versioned source")
        source_mode = mode(path)
        if source_mode & 0o111 or source_mode & 0o002:
            raise DeploymentError(
                f"versioned source permissions are not reasonable: {path} mode={source_mode:#05o}"
            )
    errors = quick_validate.validation_errors(source_dir)
    if errors:
        raise DeploymentError("Skill validation failed: " + "; ".join(errors))


def expected_live_paths() -> set[str]:
    return set(MANAGED_FILES) | EXPECTED_LIVE_DIRS


def live_paths(live_dir: Path) -> set[str]:
    if not live_dir.exists():
        return set()
    return {
        path.relative_to(live_dir).as_posix()
        for path in live_dir.rglob("*")
    }


def unknown_live_paths(live_dir: Path) -> list[str]:
    return sorted(live_paths(live_dir) - expected_live_paths())


def validate_live_container(live_dir: Path, *, allow_missing: bool) -> None:
    if not live_dir.exists():
        if allow_missing:
            return
        raise DeploymentError(f"live Skill directory is missing: {live_dir}")
    if live_dir.is_symlink() or not live_dir.is_dir():
        raise DeploymentError(f"live Skill target is not a regular directory: {live_dir}")
    if live_dir.stat().st_uid != os.getuid():
        raise DeploymentError(f"live Skill directory is not owned by the current user: {live_dir}")


def validate_existing_live_structure(live_dir: Path) -> None:
    if not live_dir.exists():
        return
    unknown = unknown_live_paths(live_dir)
    if unknown:
        raise DeploymentError(
            "live Skill contains unknown paths; preserve and classify them first: "
            + ", ".join(unknown)
        )
    for name in EXPECTED_LIVE_DIRS:
        directory = live_dir / name
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise DeploymentError(
                    f"live path is not a regular directory: {directory}"
                )
            if directory.stat().st_uid != os.getuid():
                raise DeploymentError(
                    f"live path is not owned by the current user: {directory}"
                )
    for name in MANAGED_FILES:
        target = live_dir / name
        if target.exists() or target.is_symlink():
            regular_owned_file(target, "live target")


def check(source_dir: Path, live_dir: Path) -> bool:
    validate_source(source_dir)
    validate_live_container(live_dir, allow_missing=False)
    consistent = True
    source_names = sorted(MANAGED_FILES)
    actual_live_paths = live_paths(live_dir)
    live_names = sorted(path for path in actual_live_paths if path not in EXPECTED_LIVE_DIRS)
    print("source-files " + " ".join(source_names))
    print("live-files " + " ".join(live_names))

    unknown = unknown_live_paths(live_dir)
    if unknown:
        consistent = False
        for name in unknown:
            print(f"unknown-live {name}")

    live_root_mode = mode(live_dir)
    print(f"live-directory . mode={live_root_mode:#05o}")
    if live_root_mode != 0o700:
        consistent = False
    for name in sorted(EXPECTED_LIVE_DIRS):
        directory = live_dir / name
        if directory.is_symlink() or not directory.is_dir():
            consistent = False
            print(f"drift-directory {name} state=missing-or-not-directory")
            continue
        directory_mode = mode(directory)
        print(f"live-directory {name} mode={directory_mode:#05o}")
        if directory_mode != 0o700:
            consistent = False

    for name, expected_mode in MANAGED_FILES.items():
        source = source_dir / name
        source_digest = sha256(source)
        source_mode = mode(source)
        target = live_dir / name
        if target.is_symlink():
            live_state = "symlink"
            live_mode = None
            matches = False
        elif not target.exists():
            live_state = "missing"
            live_mode = None
            matches = False
        elif not target.is_file():
            live_state = "not-regular"
            live_mode = None
            matches = False
        elif target.stat().st_uid != os.getuid():
            live_state = "wrong-owner"
            live_mode = mode(target)
            matches = False
        else:
            live_state = sha256(target)
            live_mode = mode(target)
            matches = live_state == source_digest and live_mode == expected_mode
        mode_text = "missing" if live_mode is None else f"{live_mode:#05o}"
        if matches:
            print(
                f"ok {name} source_sha256={source_digest} live_sha256={live_state} "
                f"source_mode={source_mode:#05o} live_mode={mode_text}"
            )
        else:
            consistent = False
            print(
                f"drift {name} source_sha256={source_digest} live={live_state} "
                f"source_mode={source_mode:#05o} live_mode={mode_text}"
            )
    return consistent


def ensure_live_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise DeploymentError(f"live path is not a regular directory: {path}")
        if path.stat().st_uid != os.getuid():
            raise DeploymentError(f"live path is not owned by the current user: {path}")
    else:
        path.mkdir(mode=0o700)
    path.chmod(0o700)


def install_one(source: Path, target: Path, target_mode: int) -> str:
    source_digest = sha256(source)
    if target.exists() or target.is_symlink():
        regular_owned_file(target, "live target")
        if sha256(target) == source_digest and mode(target) == target_mode:
            return f"unchanged {target.relative_to(LIVE_DIR) if LIVE_DIR in target.parents else target.name} sha256={source_digest}"

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
        temporary.chmod(target_mode)
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()

    live_digest = sha256(target)
    if live_digest != source_digest or mode(target) != target_mode:
        raise DeploymentError(f"post-install verification failed: {target}")
    return f"installed {target.name} sha256={live_digest} mode={target_mode:#05o}"


def install(source_dir: Path, live_dir: Path) -> None:
    validate_source(source_dir)
    validate_live_container(live_dir, allow_missing=True)
    validate_existing_live_structure(live_dir)

    parent = live_dir.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid():
        raise DeploymentError(f"live Skill parent is not a safe owned directory: {parent}")
    ensure_live_directory(live_dir)
    for name in sorted(EXPECTED_LIVE_DIRS):
        ensure_live_directory(live_dir / name)
    for name, target_mode in MANAGED_FILES.items():
        print(install_one(source_dir / name, live_dir / name, target_mode))
    if not check(source_dir, live_dir):
        raise DeploymentError("post-install source/live check failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check or install only the versioned Three-tool Git workflow Skill."
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
            return 0
        return 0 if check(arguments.source_dir, arguments.live_dir) else 1
    except (DeploymentError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
