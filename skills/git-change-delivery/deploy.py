#!/usr/bin/env python3
"""Check or explicitly install Git change delivery and its deprecated shim."""

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
LIVE_DIR = Path.home() / ".agents" / "skills" / "git-change-delivery"
COMPATIBILITY_SKILL_NAME = "three-tool-git-workflow"
COMPATIBILITY_SOURCE_DIR = SOURCE_DIR.parent / COMPATIBILITY_SKILL_NAME
COMPATIBILITY_LIVE_DIR = Path.home() / ".agents" / "skills" / COMPATIBILITY_SKILL_NAME
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
COMPATIBILITY_FILES = {"SKILL.md": 0o600}
# Payload-tree digests of previously released canonical Skill lines that may
# legally sit in the live directory before an upgrade. Append the released
# digest when a new canonical payload ships; never add a digest that was
# produced by local hand-editing.
RELEASED_CANONICAL_SKILL_SHA256 = frozenset(
    {
        # 1.0.0 provider-neutral payload through the pre-1.1.0 canonical line.
        "ff6d5bea2807b2c884c2ec5bee441e5fe8060abd9d03c0520c57ce39fe37adb5",
        # 1.1.0/1.1.1 released canonical payload (tag v1.1.0, tag v1.1.1).
        "3c8679b6cfd6578da41007feeea43e7ff83e8152e3daea9ab9154055642a92e2",
        # 1.2.0 released canonical payload (tag v1.2.0).
        "7b85ec9da739bd60f76736ae6352e642dbf880bbd089058cce4cf7a9c0b5c665",
        # 1.3.0 released canonical payload (tag v1.3.0, commit cfbc6d05).
        "9c05e5b279731a37b3ce15e2fbda7ae9962f81355cbafb86fa410f28ec19f527",
        # 1.4.0 and 1.5.0 shipped this canonical payload unchanged (tag v1.4.0,
        # tag v1.5.0), so a live tree holding it is released lineage.
        "0c82bceb12edde749aa6deddab25acb4ec083aeea92a37970ec64179ef92726b",
        # 1.6.0 released canonical payload (tag v1.6.0).
        "943ebf4e56ae344cf88ab3dc579d5e497663b052c8ccbade5c42f1f52b5efa93",
    }
)


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


def validate_compatibility_source(source_dir: Path) -> None:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise DeploymentError(f"compatibility Skill source is not a directory: {source_dir}")
    if source_dir.stat().st_uid != os.getuid():
        raise DeploymentError(
            f"compatibility Skill source is not owned by the current user: {source_dir}"
        )
    paths = {
        path.relative_to(source_dir).as_posix()
        for path in source_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if paths != set(COMPATIBILITY_FILES):
        raise DeploymentError(
            "compatibility Skill must contain only SKILL.md; found: "
            + ", ".join(sorted(paths))
        )
    skill = source_dir / "SKILL.md"
    regular_owned_file(skill, "compatibility source")
    content = skill.read_text(encoding="utf-8")
    required = (
        "name: three-tool-git-workflow",
        "Deprecated compatibility entry",
        "$git-change-delivery",
        "no copied workflow",
    )
    if any(marker not in content for marker in required):
        raise DeploymentError("compatibility Skill does not point exclusively to canonical Skill")
    source_mode = mode(skill)
    if source_mode & 0o111 or source_mode & 0o002:
        raise DeploymentError(
            f"compatibility source permissions are not reasonable: {skill} mode={source_mode:#05o}"
        )


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


def payload_tree_digest(directory: Path) -> str:
    lines: list[str] = []
    for relative in MANAGED_FILES:
        path = directory / relative
        regular_owned_file(path, "payload file")
        lines.append(f"{sha256(path)}  {relative}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def validate_live_content_trusted(source_dir: Path, live_dir: Path) -> None:
    if not live_dir.exists():
        return
    present = [
        name
        for name in MANAGED_FILES
        if (live_dir / name).exists() or (live_dir / name).is_symlink()
    ]
    if not present:
        return
    if len(present) != len(MANAGED_FILES):
        missing = sorted(set(MANAGED_FILES) - set(present))
        raise DeploymentError(
            "installed Skill is incomplete; preserve it and classify the missing "
            "managed files before installing: " + ", ".join(missing)
        )
    live_digest = payload_tree_digest(live_dir)
    source_digest = payload_tree_digest(source_dir)
    if live_digest == source_digest:
        return
    if live_digest in RELEASED_CANONICAL_SKILL_SHA256:
        return
    drifted = ", ".join(
        f"{name} live={sha256(live_dir / name)}"
        for name in MANAGED_FILES
        if sha256(live_dir / name) != sha256(source_dir / name)
    )
    raise DeploymentError(
        f"unknown content drift in installed Skill payload (live_tree={live_digest} "
        f"source_tree={source_digest} accepted_released_trees="
        f"{', '.join(sorted(RELEASED_CANONICAL_SKILL_SHA256))}); preserve and "
        "classify these files first, installation refuses to overwrite them: "
        + drifted
    )


def check_compatibility(source_dir: Path, live_dir: Path) -> bool:
    validate_compatibility_source(source_dir)
    validate_live_container(live_dir, allow_missing=False)
    consistent = True
    actual = live_paths(live_dir)
    if actual != set(COMPATIBILITY_FILES):
        consistent = False
        for name in sorted(actual - set(COMPATIBILITY_FILES)):
            print(f"compatibility-unknown-live {name}")
        for name in sorted(set(COMPATIBILITY_FILES) - actual):
            print(f"compatibility-missing-live {name}")
    live_root_mode = mode(live_dir)
    print(f"compatibility-live-directory . mode={live_root_mode:#05o}")
    if live_root_mode != 0o700:
        consistent = False
    for name, expected_mode in COMPATIBILITY_FILES.items():
        source = source_dir / name
        target = live_dir / name
        if target.is_symlink() or not target.is_file() or target.stat().st_uid != os.getuid():
            live_digest = "missing-or-unsafe"
            live_mode = None
            matches = False
        else:
            live_digest = sha256(target)
            live_mode = mode(target)
            matches = live_digest == sha256(source) and live_mode == expected_mode
        mode_text = "missing" if live_mode is None else f"{live_mode:#05o}"
        prefix = "compatibility-ok" if matches else "compatibility-drift"
        print(
            f"{prefix} {name} source_sha256={sha256(source)} live={live_digest} "
            f"live_mode={mode_text}"
        )
        consistent = consistent and matches
    return consistent


def check(
    source_dir: Path,
    live_dir: Path,
    compatibility_source_dir: Path = COMPATIBILITY_SOURCE_DIR,
    compatibility_live_dir: Path = COMPATIBILITY_LIVE_DIR,
) -> bool:
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
    return check_compatibility(compatibility_source_dir, compatibility_live_dir) and consistent


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


def validate_existing_compatibility_structure(live_dir: Path) -> None:
    if not live_dir.exists():
        return
    unknown = sorted(live_paths(live_dir) - set(COMPATIBILITY_FILES))
    if unknown:
        raise DeploymentError(
            "compatibility live Skill contains non-shim paths; migrate them first: "
            + ", ".join(unknown)
        )
    for name in COMPATIBILITY_FILES:
        target = live_dir / name
        if target.exists() or target.is_symlink():
            regular_owned_file(target, "compatibility live target")


def install(
    source_dir: Path,
    live_dir: Path,
    compatibility_source_dir: Path = COMPATIBILITY_SOURCE_DIR,
    compatibility_live_dir: Path = COMPATIBILITY_LIVE_DIR,
) -> None:
    validate_source(source_dir)
    validate_compatibility_source(compatibility_source_dir)
    validate_live_container(live_dir, allow_missing=True)
    validate_live_container(compatibility_live_dir, allow_missing=True)
    validate_existing_live_structure(live_dir)
    validate_live_content_trusted(source_dir, live_dir)
    validate_existing_compatibility_structure(compatibility_live_dir)

    parent = live_dir.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid():
        raise DeploymentError(f"live Skill parent is not a safe owned directory: {parent}")
    ensure_live_directory(live_dir)
    for name in sorted(EXPECTED_LIVE_DIRS):
        ensure_live_directory(live_dir / name)
    for name, target_mode in MANAGED_FILES.items():
        print(install_one(source_dir / name, live_dir / name, target_mode))
    ensure_live_directory(compatibility_live_dir)
    for name, target_mode in COMPATIBILITY_FILES.items():
        print(
            install_one(
                compatibility_source_dir / name,
                compatibility_live_dir / name,
                target_mode,
            )
        )
    if not check(source_dir, live_dir, compatibility_source_dir, compatibility_live_dir):
        raise DeploymentError("post-install source/live check failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check or install Git change delivery and its deprecated compatibility shim."
    )
    parser.add_argument("action", choices=("check", "install"))
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    parser.add_argument(
        "--compatibility-source-dir", type=Path, default=COMPATIBILITY_SOURCE_DIR
    )
    parser.add_argument(
        "--compatibility-live-dir", type=Path, default=COMPATIBILITY_LIVE_DIR
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.action == "install":
            install(
                arguments.source_dir,
                arguments.live_dir,
                arguments.compatibility_source_dir,
                arguments.compatibility_live_dir,
            )
            return 0
        return (
            0
            if check(
                arguments.source_dir,
                arguments.live_dir,
                arguments.compatibility_source_dir,
                arguments.compatibility_live_dir,
            )
            else 1
        )
    except (DeploymentError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
