#!/usr/bin/env python3
"""Release preparation and verification for Git Finalizer.

Single implementation shared by every release host, so a gate cannot exist on one host and be
missing on another. Subcommands:

  preflight <tag>       verify the version batch and the hash-bound files
  build <tag>           stage the package, build it deterministically, verify it, write checksums
  verify <tag>          verify a built artifact against the checksums `build` recorded
  selfcheck <tag>       build twice entirely inside a temporary directory and report the digest

The build recipe is fixed: ustar, sorted members, owner/group 0, numeric owner ids, mtime pinned to
the commit timestamp, and gzip without a name or timestamp. Two builds of one commit must be
byte-identical, and the file set must equal the installation contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tooling import tool_skill_sync as sync  # noqa: E402  (path setup above)

COMPANIONS = (
    "git-finalize-integration-publish.py",
    "git-finalize-repo-bootstrap.py",
    "git-finalize-retirement-plan.py",
)
METADATA_FILES = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "tool_cli_contract.json",
    "tool_skill_manifest.json",
    "toolchain_compatibility.json",
)
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+"


class ReleaseError(Exception):
    """A release precondition is not met; nothing was published."""


def package_name(version: str) -> str:
    return f"git-finalizer-{version}"


def package_paths() -> set[str]:
    manifest = sync.read_json(ROOT / "tool_skill_manifest.json")
    paths = set(manifest["executable"]["paths"])
    paths.update(METADATA_FILES)
    paths.update(f"skills/{sync.SKILL_NAME}/{p}" for p in sync.SKILL_PAYLOAD)
    paths.add(manifest["compatibility_skill"]["path"])
    paths.update(
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "manifests").glob("*/tool_skill_manifest.json"))
    )
    return paths


def read_version_literal(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ReleaseError(f"cannot read a version literal from {path}")
    return match.group(1)


def preflight(version_tag: str) -> str:
    if re.fullmatch(rf"v({VERSION_PATTERN})", version_tag) is None:
        raise ReleaseError(f"invalid release tag: {version_tag}")
    version = version_tag[1:]
    script_version = read_version_literal(
        ROOT / "git-finalize", r'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$'
    )
    readme_version = read_version_literal(
        ROOT / "README.md", r"^当前版本：`([0-9]+\.[0-9]+\.[0-9]+)`$"
    )
    if script_version != readme_version:
        raise ReleaseError("Finalizer version declarations disagree")
    if version_tag != f"v{script_version}":
        raise ReleaseError(f"tag {version_tag} does not match Finalizer version {script_version}")
    for companion in COMPANIONS:
        companion_version = read_version_literal(
            ROOT / companion, r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$'
        )
        if companion_version != version:
            raise ReleaseError(f"companion declares a different version: {companion}")
    contract = sync.read_json(ROOT / "tool_cli_contract.json")
    manifest = sync.read_json(ROOT / "tool_skill_manifest.json")
    compatibility = sync.read_json(ROOT / "toolchain_compatibility.json")
    if contract["tool_version"] != version:
        raise ReleaseError("CLI contract declares a different version")
    if manifest["tool_version"] != version:
        raise ReleaseError("root manifest declares a different version")
    if contract["contract_version"] != compatibility["git_finalizer_contract_version"]:
        raise ReleaseError("CLI contract version disagrees with toolchain compatibility")
    contract_digest = sync.sha256_file(ROOT / "tool_cli_contract.json")
    if manifest["public_cli_contract_sha256"] != contract_digest:
        raise ReleaseError("root manifest does not bind the shipped CLI contract bytes")
    payload_digest = sync.tree_sha256(ROOT / "skills" / sync.SKILL_NAME, sync.SKILL_PAYLOAD)
    manifests = [ROOT / "tool_skill_manifest.json"]
    manifests.extend(sorted((ROOT / "manifests").glob("*/tool_skill_manifest.json")))
    for path in manifests:
        bound = sync.read_json(path)["canonical_skill_sha256"]
        if bound != payload_digest:
            raise ReleaseError(
                f"{path.relative_to(ROOT)} does not bind the shipped Skill payload {payload_digest}"
            )
    if not (ROOT / "LICENSE").is_file():
        raise ReleaseError("LICENSE is missing from the released package surface")
    return version


def stage(stage_root: Path) -> None:
    stage_root.mkdir(parents=True, exist_ok=True)
    for relative in sorted(package_paths()):
        source = ROOT / relative
        if not source.is_file():
            raise ReleaseError(f"packaged file is missing: {relative}")
        target = stage_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(stat.S_IMODE(source.stat().st_mode) & 0o755)
    # Directory modes must not depend on the builder's umask, or two hosts produce
    # different bytes from the same commit.
    for directory in sorted(stage_root.rglob("*"), key=lambda p: str(p)):
        if directory.is_dir():
            directory.chmod(0o755)
    stage_root.chmod(0o755)


def commit_timestamp() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "show", "-s", "--format=%ct", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ReleaseError("cannot read the commit timestamp for reproducible mtime")
    return result.stdout.strip()


def tar_bytes(stage_root: Path, package: str, timestamp: str) -> bytes:
    command = [
        "tar",
        "--sort=name",
        "--format=ustar",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        f"--mtime=@{timestamp}",
        "-C",
        str(stage_root),
        "-cf",
        "-",
        package,
    ]
    archive = subprocess.run(command, capture_output=True, check=True).stdout
    gzip = subprocess.run(
        ["gzip", "-n"], input=archive, capture_output=True, check=True
    ).stdout
    return gzip


def build(version_tag: str, dist: Path | None = None, staging: Path | None = None) -> Path:
    version = version_tag[1:]
    package = package_name(version)
    dist_root = dist or ROOT / "dist"
    stage_root = staging or ROOT / ".release-stage"
    for path in (dist_root, stage_root):
        if path.exists():
            shutil.rmtree(path)
    dist_root.mkdir(parents=True, exist_ok=True)
    stage(stage_root / package)
    timestamp = commit_timestamp()
    artifact = dist_root / f"{package}.tar.gz"
    artifact.write_bytes(tar_bytes(stage_root, package, timestamp))
    rebuild = stage_root / f"{package}.rebuild.tar.gz"
    rebuild.write_bytes(tar_bytes(stage_root, package, timestamp))
    if artifact.read_bytes() != rebuild.read_bytes():
        raise ReleaseError("release build is not deterministic for one commit")
    verify_bytes(artifact, version_tag)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (dist_root / "SHA256SUMS.txt").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    return artifact


def verify_bytes(artifact: Path, version_tag: str) -> None:
    version = version_tag[1:]
    package = package_name(version)
    expected = {f"{package}/{p}" for p in package_paths()}
    try:
        archive = tarfile.open(artifact, "r:gz")
    except tarfile.TarError as error:
        raise ReleaseError(f"release artifact is not a readable gzip tarball: {error}") from error
    with archive:
        members = archive.getmembers()
        files = {member.name for member in members if member.isfile()}
        if files != expected:
            missing = sorted(expected - files)
            extra = sorted(files - expected)
            raise ReleaseError(
                "release package file set does not match the installation contract "
                f"(missing={missing} extra={extra})"
            )
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ReleaseError(f"release package contains an unsafe path: {member.name}")
        entry = archive.getmember(f"{package}/git-finalize")
        if stat.S_IMODE(entry.mode) != 0o755:
            raise ReleaseError("Finalizer entry point is not executable in the release package")
        for member in members:
            if member.isdir() and stat.S_IMODE(member.mode) != 0o755:
                raise ReleaseError(
                    "release package directory mode depends on the builder umask: "
                    f"{member.name} mode={stat.S_IMODE(member.mode):#o}"
                )
        stream = archive.extractfile(entry)
        if stream is None:
            raise ReleaseError("Finalizer entry point is unreadable in the release package")
        match = re.search(
            rb'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$', stream.read(), re.MULTILINE
        )
        if match is None or match.group(1).decode() != version:
            raise ReleaseError("release package contains the wrong Finalizer version")


def verify(version_tag: str, dist: Path | None = None) -> str:
    version = version_tag[1:]
    package = package_name(version)
    dist_root = dist or ROOT / "dist"
    artifact = dist_root / f"{package}.tar.gz"
    if not artifact.is_file():
        raise ReleaseError(f"release artifact is missing: {artifact}")
    verify_bytes(artifact, version_tag)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksums = dist_root / "SHA256SUMS.txt"
    if not checksums.is_file():
        raise ReleaseError(
            f"{checksums.name} is missing next to the artifact; only `build` may author a "
            "checksum file, otherwise verification would grade its own homework"
        )
    recorded = checksums.read_text(encoding="utf-8").split()
    if not recorded:
        raise ReleaseError(f"{checksums.name} records no digest")
    if recorded[0] != digest:
        raise ReleaseError("SHA256SUMS.txt does not match the built artifact bytes")
    check = subprocess.run(
        ["sha256sum", "--check", "SHA256SUMS.txt"],
        cwd=dist_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        raise ReleaseError(f"checksum verification failed: {check.stdout}{check.stderr}")
    return digest


def selfcheck(version_tag: str) -> str:
    """Build twice into scratch space and report the digest; writes nothing into the repository."""
    with tempfile.TemporaryDirectory(prefix="git-finalizer-selfcheck-") as temporary:
        scratch = Path(temporary)
        first = build(version_tag, dist=scratch / "one", staging=scratch / "stage-one")
        second = build(version_tag, dist=scratch / "two", staging=scratch / "stage-two")
        digest_one = hashlib.sha256(first.read_bytes()).hexdigest()
        digest_two = hashlib.sha256(second.read_bytes()).hexdigest()
        if digest_one != digest_two:
            raise ReleaseError("two selfcheck builds differ")
        return digest_one


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("preflight", "build", "verify", "selfcheck"))
    parser.add_argument("tag", help="release tag, for example v1.4.0")
    arguments = parser.parse_args(argv)
    try:
        if arguments.action == "preflight":
            print(f"release_version={preflight(arguments.tag)}")
        elif arguments.action == "build":
            artifact = build(arguments.tag)
            print(f"built={artifact} sha256={hashlib.sha256(artifact.read_bytes()).hexdigest()}")
        elif arguments.action == "verify":
            print(f"verified sha256={verify(arguments.tag)}")
        else:
            print(f"selfcheck sha256={selfcheck(arguments.tag)}")
    except (ReleaseError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
